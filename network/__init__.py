"""Socket and network-impairment layer.

    udp.py         UDP socket creation; the Windows ICMP-reset quirk          ✅
    simulator.py   ImpairedSocket: loss, one-way delay, jitter, seeded RNG   ✅

Impairment is strictly optional: with 0% loss and 0 delay the simulated socket
must behave identically to a raw UDP socket (T4.1).
"""
