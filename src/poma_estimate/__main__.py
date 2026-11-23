import os
import signal
import sys

# The one-file binary also runs as each worker process and as multiprocessing's resource tracker.
# Those leave Ctrl+C to the scanner, which stops and reports the estimate so far; ignore it from
# the very start, before the imports below, so a key press during their start-up cannot kill them.
if len(sys.argv) > 1 and (sys.argv[1] == "--multiprocessing-fork"
                          or ("-c" in sys.argv and any(a.startswith("from multiprocessing.") for a in sys.argv))):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
else:
    def _interrupt_once(signum, frame):
        """The one-file launcher forwards Ctrl+C, so one key press arrives twice: the first stops,
        the second is ignored (it would otherwise hit the handler of the first)."""
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _interrupt_once)

try:
    from poma_estimate.cli import run
except KeyboardInterrupt:  # Ctrl+C while the one-file binary is still starting up
    sys.stderr.write("stopped\n")
    sys.stderr.flush()
    os._exit(130)

run()
