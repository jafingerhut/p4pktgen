# TODO:
# - Print out which values were used for constraints.

from collections import defaultdict
import logging
from contextlib import contextmanager
import time

from z3 import *

from p4pktgen.config import Config


class ContextVar(object):
    def __init__(self):
        pass


class Field(ContextVar):
    def __init__(self, header, field):
        super(ContextVar, self).__init__()
        self.header = header
        self.field = field

    def __eq__(self, other):
        return self.header == other.header and self.header == other.header


# XXX: This class needs some heavy refactoring.
class Context:
    """The context that is used to generate the symbolic representation of a P4
    program."""

    next_id = 0

    def __init__(self):
        # Maps variables to a list of versions
        self.var_to_smt_var = {}
        self.var_to_smt_val = {}

        self.new_vars = set()

        self.fields = {}
        # XXX: unify errors
        self.uninitialized_reads = []
        self.invalid_header_writes = []
        self.input_metadata = {}
        self.source_info = None

        # Stores references to smt_vars of header fields used to key into each
        # table on the path.
        # TODO: Consolidate table fields into one dict.
        self.table_key_values = {}  # {table_name: [key_smt_var, ...]}
        # Stores references to smt_vars of table runtime data (action params)
        # used in each table-action on the path.
        self.table_runtime_data = {}  # {table_name: [param_smt_var, ...]}
        # Each table can only be hit once per path.
        self.table_action = {}  # {table_name: action_name}
        # Temporary variable only populated while translating a table action
        self.current_table_name = None

        self.parsed_stacks = defaultdict(int)
        self.parsed_vl_extracts = {}

    def __copy__(self):
        context_copy = Context()
        context_copy.__dict__.update(self.__dict__)
        context_copy.fields = dict.copy(self.fields)
        context_copy.uninitialized_reads = list(self.uninitialized_reads)
        context_copy.invalid_header_writes = list(self.invalid_header_writes)
        context_copy.new_vars = set(self.new_vars)
        context_copy.var_to_smt_var = dict.copy(self.var_to_smt_var)
        context_copy.var_to_smt_val = dict.copy(self.var_to_smt_val)
        context_copy.table_key_values = dict.copy(self.table_key_values)
        context_copy.table_runtime_data = dict.copy(self.table_runtime_data)
        context_copy.table_action = dict.copy(self.table_action)
        context_copy.input_metadata = dict.copy(self.input_metadata)
        return context_copy

    def set_source_info(self, source_info):
        self.source_info = source_info

    def unset_source_info(self):
        self.source_info = None

    def register_field(self, field):
        self.fields[self.field_to_var(field)] = field

    def fresh_var(self, prefix):
        Context.next_id += 1
        return '{}_{}'.format(prefix, Context.next_id)

    def field_to_var(self, field):
        assert field.header is not None
        return (field.header.name, field.name)

    def insert(self, field, sym_val):
        self.set_field_value(field.header.name, field.name, sym_val)

    def set_field_value(self, header_name, header_field, sym_val):
        var_name = (header_name, header_field)
        do_write = True

        # XXX: clean up
        if header_field != '$valid$' and (header_name,
                                          '$valid$') in self.var_to_smt_var:
            smt_var_valid = self.var_to_smt_var[(header_name, '$valid$')]
            if simplify(self.var_to_smt_val[smt_var_valid]) == BitVecVal(0, 1):
                if Config().get_allow_invalid_header_writes():
                    do_write = False
                else:
                    self.invalid_header_writes.append((var_name,
                                                       self.source_info))

        if do_write:
            Context.next_id += 1
            new_smt_var = BitVec('{}.{}.{}'.format(var_name[0], var_name[1],
                                                   Context.next_id),
                                 sym_val.size())
            self.new_vars.add(new_smt_var)
            self.var_to_smt_var[var_name] = new_smt_var
            self.var_to_smt_val[new_smt_var] = sym_val

    def set_table_action(self, table_name, action_name):
        if table_name in self.table_action:
            assert self.table_action[table_name] == action_name
        else:
            self.table_action[table_name] = action_name

    def add_runtime_data(self, table_name, action_name, params):
        assert table_name not in self.table_runtime_data
        runtime_data = []
        for i, (param_name, param_bitwidth) in enumerate(params):
            # XXX: can actions call other actions? This won't work in that case
            param_smt_var = BitVec('${}$.${}$.runtime_data_{}'.format(
                table_name, action_name, param_name), param_bitwidth)
            self.set_field_value('{}.{}.{}'.format(table_name, action_name,
                                                   param_name),
                                 str(i), param_smt_var)
            runtime_data.append(param_smt_var)
        self.table_runtime_data[table_name] = runtime_data

    def get_table_runtime_data(self, table_name, idx):
        return self.table_runtime_data[table_name][idx]

    @contextmanager
    def set_current_table(self, table_name):
        # Temporarily sets current table, for converting table primitives.
        try:
            self.current_table_name = table_name
            yield
        finally:
            self.current_table_name = None

    def get_current_table_runtime_data(self, idx):
        # Must be called inside a with set_current_table context
        assert self.current_table_name is not None
        return self.get_table_runtime_data(self.current_table_name, idx)

    def remove_header_fields(self, header_name):
        # XXX: hacky
        for k in list(self.var_to_smt_var.keys()):
            if len(k) == 2 and k[0] == header_name and not k[1] == '$valid$':
                Context.next_id += 1
                self.var_to_smt_var[k] = None

    def set_table_key_values(self, table_name, sym_key_vals):
        self.table_key_values[table_name] = sym_key_vals

    def get_table_key_values(self, model, table_name):
        return [
            model.eval(sym_val, model_completion=True)
            for sym_val in self.table_key_values[table_name]
        ]

    def has_table_values(self, table_name):
        return table_name in self.table_key_values

    def get(self, field):
        return self.get_var(self.field_to_var(field))

    def get_header_field(self, header_name, header_field):
        return self.get_var((header_name, header_field))

    def get_header_field_size(self, header_name, header_field):
        return self.get_header_field(header_name, header_field).size()

    def get_last_header_field(self, header_name, header_field, size):
        # XXX: size should not be a param

        last_valid = None
        for i in range(size):
            smt_var_valid = self.var_to_smt_var[('{}[{}]'.format(
                header_name, i), '$valid$')]
            if simplify(self.var_to_smt_val[smt_var_valid]) == BitVecVal(1, 1):
                last_valid = i

        # XXX: check for all invalid

        return self.get_header_field('{}[{}]'.format(header_name, last_valid),
                                     header_field)

    def get_var(self, var_name):
        if var_name not in self.var_to_smt_var:
            # The variable that we're reading has not been set by the program.
            field = self.fields[var_name]
            new_var = BitVec(self.fresh_var(var_name), field.size)
            if field.hdr.metadata and Config().get_solve_for_metadata():
                # We're solving for metadata.  Set the field to an
                # unconstrained value.
                self.set_field_value(var_name[0], var_name[1], new_var)
                self.input_metadata[var_name] = new_var
            elif Config().get_allow_uninitialized_reads():
                # Read the uninitialized value as zero.
                return BitVecVal(0, field.size)
            else:
                # If the header field has not been initialized, return a
                # fresh variable for each read access
                self.uninitialized_reads.append((var_name, self.source_info))
                return new_var

        assert var_name in self.var_to_smt_var
        return self.var_to_smt_var[var_name]

    def get_name_constraints(self):
        var_constraints = []
        for var in self.new_vars:
            var_constraints.append(var == self.var_to_smt_val[var])
        self.new_vars = set()
        return var_constraints

    def record_extract_vl(self, header_name, header_field, sym_size):
        var = (header_name, header_field)
        assert var not in self.parsed_vl_extracts
        self.parsed_vl_extracts[var] = sym_size

    def log_constraints(self):
        var_constraints = self.get_name_constraints()
        logging.info('Variable constraints')
        for constraint in var_constraints:
            logging.info('\t{}'.format(constraint))

    def get_stack_next_header_name(self, header_name):
        # Each element in a stack needs a unique name, generate them in order
        # of extraction.
        name = '{}[{}]'.format(header_name, self.parsed_stacks[header_name])
        self.parsed_stacks[header_name] += 1
        return name

    def set_valid_field(self, header_name):
        # Even though the P4_16 isValid() method
        # returns a boolean value, it appears that
        # when p4c-bm2-ss compiles expressions like
        # "if (ipv4.isValid())" into a JSON file, it
        # compares the "ipv4.$valid$" field to a bit
        # vector value of 1 with the == operator, thus
        # effectively treating the "ipv4.$valid$" as
        # if it is a bit<1> type.
        self.set_field_value(header_name, '$valid$',
                              BitVecVal(1, 1))
