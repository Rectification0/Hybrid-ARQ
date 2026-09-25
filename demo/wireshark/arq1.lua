-- Wireshark dissector for the demo's ARQ1 format (demo/common/packet.py).
-- Decodes the demo only: the real protocol ("HA" magic, 21-byte header, port 8888) is
-- not covered. Load with:  wireshark -X lua_script:demo/wireshark/arq1.lua
--
-- Header "!4sBIHH", 13 bytes, big-endian:
--   magic "ARQ1" (4) | type (1) | seq (4) | checksum (2) | payload length (2) | payload

local arq1 = Proto("arq1", "ARQ1 teaching demo (GBN / SR)")

local TYPES = { [1] = "DATA", [2] = "ACK" }
local HEADER_LEN = 13

local f_magic    = ProtoField.string("arq1.magic", "Magic")
local f_type     = ProtoField.uint8("arq1.type", "Type", base.DEC, TYPES)
local f_seq      = ProtoField.uint32("arq1.seq", "Sequence", base.DEC)
local f_checksum = ProtoField.uint16("arq1.checksum", "Checksum", base.HEX)
local f_len      = ProtoField.uint16("arq1.len", "Payload length", base.DEC)
local f_payload  = ProtoField.bytes("arq1.payload", "Payload")
arq1.fields = { f_magic, f_type, f_seq, f_checksum, f_len, f_payload }

local e_checksum = ProtoExpert.new("arq1.checksum.bad", "Checksum mismatch",
                                   expert.group.CHECKSUM, expert.severity.WARN)
local e_length   = ProtoExpert.new("arq1.len.bad", "Payload length does not match datagram",
                                   expert.group.MALFORMED, expert.severity.WARN)
arq1.experts = { e_checksum, e_length }

-- Same 16-bit ones'-complement sum as checksum() in packet.py, computed over
-- magic | type | seq | 0x0000 | payload (the length field is not covered).
-- Plain arithmetic instead of bit operators, so it runs on Lua 5.2 and 5.4 alike.
local function demo_checksum(buf)
    local bytes = {}
    for i = 0, 8 do bytes[#bytes + 1] = buf(i, 1):uint() end
    bytes[#bytes + 1] = 0
    bytes[#bytes + 1] = 0
    for i = HEADER_LEN, buf:len() - 1 do bytes[#bytes + 1] = buf(i, 1):uint() end
    if #bytes % 2 == 1 then bytes[#bytes + 1] = 0 end

    local total = 0
    for i = 1, #bytes, 2 do
        total = total + bytes[i] * 256 + bytes[i + 1]
        total = (total % 65536) + math.floor(total / 65536)
    end
    return 65535 - (total % 65536)
end

function arq1.dissector(buf, pinfo, tree)
    if buf:len() < HEADER_LEN or buf(0, 4):string() ~= "ARQ1" then
        return 0  -- not ours; leave it to Wireshark
    end

    local ptype = buf(4, 1):uint()
    local seq   = buf(5, 4):uint()
    local plen  = buf(11, 2):uint()
    local name  = TYPES[ptype] or ("TYPE " .. ptype)

    pinfo.cols.protocol = "ARQ1"
    local info = name .. " seq=" .. seq
    if ptype == 1 then info = info .. " len=" .. plen end
    pinfo.cols.info = info

    local subtree = tree:add(arq1, buf(), "ARQ1 demo, " .. info)
    subtree:add(f_magic, buf(0, 4))
    subtree:add(f_type, buf(4, 1))
    subtree:add(f_seq, buf(5, 4))

    local checksum_item = subtree:add(f_checksum, buf(9, 2))
    if demo_checksum(buf) == buf(9, 2):uint() then
        checksum_item:append_text(" [correct]")
    else
        checksum_item:append_text(" [incorrect]")
        checksum_item:add_proto_expert_info(e_checksum)
    end

    local len_item = subtree:add(f_len, buf(11, 2))
    if buf:len() - HEADER_LEN ~= plen then
        len_item:add_proto_expert_info(e_length)
    end
    if buf:len() > HEADER_LEN then
        subtree:add(f_payload, buf(HEADER_LEN))
    end
    return buf:len()
end

-- The demo's ports only (GBN 5000, SR 5001).
local udp_port = DissectorTable.get("udp.port")
udp_port:add(5000, arq1)
udp_port:add(5001, arq1)
