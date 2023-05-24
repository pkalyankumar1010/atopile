import contextlib
import cProfile
import logging
import pstats
import time
from pathlib import Path

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


def get_project_root():
    return Path(__file__).parent.parent.parent

class StreamToLogger(object):
    """
    Fake file-like stream object that redirects writes to a logger instance.
    """
    def __init__(self, logger: logging.Logger, log_level: int=logging.INFO):
        self.logger = logger
        self.log_level = log_level
        self.linebuf = ''

    def write(self, buf):
        temp_linebuf: str = self.linebuf + buf
        self.linebuf = ''
        for line in temp_linebuf.splitlines(True):
            # From the io.TextIOWrapper docs:
            #   On output, if newline is None, any '\n' characters written
            #   are translated to the system default line separator.
            # By default sys.stdout.write() expects '\n' newlines and then
            # translates them so this is still cross platform.
            if line[-1] == '\n':
                self.logger.log(self.log_level, line.rstrip())
            else:
                self.linebuf += line

    def flush(self):
        if self.linebuf != '':
            self.logger.log(self.log_level, self.linebuf.rstrip())
        self.linebuf = ''

@contextlib.contextmanager
def profile(profile_log: logging.Logger, entries: int=20, sort_stats="cumtime", skip=False):
    if skip:
        # skip allows you to include the profiler context in code and switch it easily downstream
        yield
        return

    prof = cProfile.Profile()
    prof.enable()
    start_time = time.time()
    log.info("Running profiler...")
    yield
    log.info(f"Finished profiling. Took {time.time() - start_time} seconds.")
    prof.disable()
    s = StreamToLogger(profile_log, logging.DEBUG)
    stats = pstats.Stats(prof, stream=s).sort_stats(sort_stats)
    stats.print_stats(entries)
