"""Helper functions for logging progress."""

import time

def get_current_time():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())