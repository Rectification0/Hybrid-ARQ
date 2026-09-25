import os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
# each script line-buffers its stdout, so sender and receiver lines interleave cleanly
r = subprocess.Popen([sys.executable, os.path.join(HERE, "gbn", "receiver.py")])
time.sleep(0.5)
try:
    subprocess.run([sys.executable, os.path.join(HERE, "gbn", "sender.py"), *sys.argv[1:]],
                   check=True)
finally:
    time.sleep(0.2)  # let the receiver print its last ACK line
    r.terminate()    # the GBN receiver has no end condition of its own
