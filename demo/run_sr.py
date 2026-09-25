import os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
# each script line-buffers its stdout, so sender and receiver lines interleave cleanly
r = subprocess.Popen([sys.executable, os.path.join(HERE, "sr", "receiver.py")])
time.sleep(0.5)
try:
    subprocess.run([sys.executable, os.path.join(HERE, "sr", "sender.py"), *sys.argv[1:]],
                   check=True)
finally:
    try:
        r.wait(timeout=2)  # the SR receiver exits by itself once all segments are delivered
    except subprocess.TimeoutExpired:
        r.terminate()
