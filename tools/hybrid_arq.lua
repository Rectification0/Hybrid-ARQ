--
-- Wireshark/tshark dissector for the Hybrid GBN/SR ARQ wire format (T9.5).
--
-- specs.md §22 calls a dissector an *optional* presentation enhancement: the
-- header is a fixed 21-byte prefix, so sequence and ACK are already readable at
-- constant offsets in the bytes pane without one (WS-05). What it adds is that
-- the evidence becomes *filterable* — `hybridarq.type == 1 && hybridarq.seq == 42`
-- finds every transmission of one segment, which is how a retransmission pattern
-- is demonstrated rather than asserted (WS-06, WS-07).
--
-- It also gives `experiments/capture_evidence.py` a way to summarise a capture
-- by field rather than by eye, so each committed .pcapng ships with counts that
-- a reader can reproduce.
--
-- The layout is D1, frozen at T1.1 (specs.md §5, §16.1); this file must change
-- only if that decision is ever unfrozen — which would invalidate every capture
-- already recorded.
--
--     offset 0      2   3    4      5         9     13      15              17
--            MAGIC  VER TYPE FLAGS  SEQUENCE  ACK   WINDOW  PAYLOAD_LENGTH  CHECKSUM
--            2B     1B  1B   1B     4B        4B    2B      2B              4B
--
-- Usage:
--     tshark -r capture.pcapng -X lua_script:tools/hybrid_arq.lua \
--            -Y 'hybridarq' -T fields -e hybridarq.type_name -e hybridarq.seq
--
-- or copy it into the Wireshark personal plugins directory to load it in the GUI.

local hybridarq = Proto("hybridarq", "Hybrid GBN/SR ARQ")

local MAGIC = 0x4841            -- ASCII "HA"
local HEADER_SIZE = 21

local types = {
    [1] = "DATA",
    [2] = "ACK",
    [3] = "START",
    [4] = "START_ACK",
    [5] = "FIN",
    [6] = "FIN_ACK",
    [7] = "MODE",
}

local f_magic    = ProtoField.uint16("hybridarq.magic", "Magic", base.HEX)
local f_version  = ProtoField.uint8("hybridarq.version", "Version", base.DEC)
local f_type     = ProtoField.uint8("hybridarq.type", "Type", base.DEC, types)
local f_typename = ProtoField.string("hybridarq.type_name", "Type name")
local f_flags    = ProtoField.uint8("hybridarq.flags", "Flags", base.HEX)
local f_seq      = ProtoField.uint32("hybridarq.seq", "Sequence", base.DEC)
local f_ack      = ProtoField.uint32("hybridarq.ack", "Ack", base.DEC)
local f_window   = ProtoField.uint16("hybridarq.window", "Window", base.DEC)
local f_length   = ProtoField.uint16("hybridarq.payload_length", "Payload length", base.DEC)
local f_checksum = ProtoField.uint32("hybridarq.checksum", "Checksum (CRC-32)", base.HEX)
local f_payload  = ProtoField.bytes("hybridarq.payload", "Payload")
local f_control  = ProtoField.string("hybridarq.control", "Control payload (JSON)")

hybridarq.fields = {
    f_magic, f_version, f_type, f_typename, f_flags, f_seq, f_ack,
    f_window, f_length, f_checksum, f_payload, f_control,
}

-- Captures are taken with a snaplen on the larger runs, so a datagram may be
-- truncated after the header. That is deliberate: every field above still
-- resolves, and only the payload bytes are missing. The dissector therefore
-- works from `buffer:len()` rather than assuming the whole payload is present.
function hybridarq.dissector(buffer, pinfo, tree)
    local available = buffer:len()
    if available < HEADER_SIZE then return 0 end
    if buffer(0, 2):uint() ~= MAGIC then return 0 end

    local ptype = buffer(3, 1):uint()
    local name = types[ptype] or string.format("UNKNOWN(%d)", ptype)
    local length = buffer(15, 2):uint()

    pinfo.cols.protocol = "HybridARQ"
    if ptype == 1 then
        pinfo.cols.info = string.format("DATA seq=%d len=%d", buffer(5, 4):uint(), length)
    elseif ptype == 2 then
        pinfo.cols.info = string.format("ACK ack=%d", buffer(9, 4):uint())
    else
        pinfo.cols.info = name
    end

    local subtree = tree:add(hybridarq, buffer(), "Hybrid GBN/SR ARQ, " .. name)
    subtree:add(f_magic, buffer(0, 2))
    subtree:add(f_version, buffer(2, 1))
    subtree:add(f_type, buffer(3, 1))
    subtree:add(f_typename, buffer(3, 1), name)
    subtree:add(f_flags, buffer(4, 1))
    subtree:add(f_seq, buffer(5, 4))
    subtree:add(f_ack, buffer(9, 4))
    subtree:add(f_window, buffer(13, 2))
    subtree:add(f_length, buffer(15, 2))
    subtree:add(f_checksum, buffer(17, 4))

    local present = math.min(length, available - HEADER_SIZE)
    if present > 0 then
        local payload = buffer(HEADER_SIZE, present)
        subtree:add(f_payload, payload)
        -- Control payloads are JSON (design.md §5.3); showing the text makes a
        -- MODE handshake readable in the packet list, which is the whole point
        -- of capturing a transition (WS-08).
        if ptype >= 3 and present == length then
            subtree:add(f_control, payload, payload:string())
        end
    end
    return HEADER_SIZE + present
end

-- 8888 is the fixed development port (WS-02); heuristic registration catches a
-- run on any other port, which the experiment runner may use.
DissectorTable.get("udp.port"):add(8888, hybridarq)

local function heuristic(buffer, pinfo, tree)
    if buffer:len() < HEADER_SIZE or buffer(0, 2):uint() ~= MAGIC then
        return false
    end
    hybridarq.dissector(buffer, pinfo, tree)
    return true
end

hybridarq:register_heuristic("udp", heuristic)
