#!/usr/bin/env python3
# Toy self-checkpointing job: count to N, checkpoint every STEP seconds.
import os, sys, time
N, STEP, CKPT = int(sys.argv[1]), int(sys.argv[2]), "count.ckpt"
i = int(open(CKPT).read()) if os.path.exists(CKPT) else 0
print(f"start i={i} host={os.uname().nodename}", flush=True)
deadline = time.time() + STEP
while i < N:
    time.sleep(1)
    i += 1
    if time.time() >= deadline:
        with open(CKPT + ".tmp", "w") as f:
            f.write(str(i))
        os.replace(CKPT + ".tmp", CKPT)
        print(f"ckpt i={i} host={os.uname().nodename}", flush=True)
        sys.exit(85)
print(f"done i={i} host={os.uname().nodename}", flush=True)
