/*
Copyright 2017 Cisco Systems, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

#include <core.p4>
#include <v1model.p4>

typedef bit<48>  EthernetAddress;
typedef bit<32>  IPv4Address;

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

// IPv4 header _with_ options
header ipv4_t {
    bit<4>       version;
    bit<4>       ihl;
    bit<8>       diffserv;
    bit<16>      totalLen;
    bit<16>      identification;
    bit<3>       flags;
    bit<13>      fragOffset;
    bit<8>       ttl;
    bit<8>       protocol;
    bit<16>      hdrChecksum;
    IPv4Address  srcAddr;
    IPv4Address  dstAddr;
}

struct headers {
    ethernet_t    ethernet;
    ipv4_t        ipv4;
}

struct metadata {
    bit<8>   packet_color;
    // I would normally prefer in P4_16 to make meter_drop type bool
    // instead of bit<1>, but currently there is a p4c bug (issue
    // #1049) that causes the bmv2 JSON created to be incorrect for
    // such a program.  Hopefully I can temporarily work around that
    // bug by using bit<1> instead.
    bit<1>   meter_drop;
}

parser parserI(packet_in pkt,
               out headers hdr,
               inout metadata meta,
               inout standard_metadata_t stdmeta)
{
    state start {
        pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            0x0800: parse_ipv4;
            default: accept;
        }
    }
    state parse_ipv4 {
        pkt.extract(hdr.ipv4);
        transition accept;
    }
}

control cIngress(inout headers hdr,
                 inout metadata meta,
                 inout standard_metadata_t stdmeta)
{
    meter(32w128, MeterType.bytes) my_meter;
    action meter_drop_decision(bit<7> meter_id, bit<8> p4pktgen_hack_packet_color) {
        //my_meter.execute_meter((bit<32>) meter_id, meta.packet_color);
        meta.packet_color = p4pktgen_hack_packet_color;
        meta.meter_drop = (meta.packet_color == 1) ? (bit<1>) 1 : (bit<1>) 0;
    }
    table drop_decision {
        key = {
            hdr.ipv4.dstAddr : exact;
        }
        actions = {
            meter_drop_decision;
        }
        default_action = meter_drop_decision(0, 0);
    }
    apply {
        if (hdr.ipv4.isValid()) {
            meta.meter_drop = 0;
            drop_decision.apply();
            if (meta.meter_drop == 1) {
                mark_to_drop();
                exit;
            }
        }
    }
}

control cEgress(inout headers hdr,
                inout metadata meta,
                inout standard_metadata_t stdmeta)
{
    apply {
    }
}

control verifyChecksum(inout headers hdr,
                       inout metadata meta)
{
    apply {
    }
}

control updateChecksum(inout headers hdr,
                       inout metadata meta)
{
    apply {
    }
}

control DeparserI(packet_out packet,
                  in headers hdr)
{
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);
    }
}

V1Switch<headers, metadata>(parserI(),
                            verifyChecksum(),
                            cIngress(),
                            cEgress(),
                            updateChecksum(),
                            DeparserI()) main;
