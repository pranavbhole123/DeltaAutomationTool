"""Save discovery evidence to the log and notify optional progress observers."""
import logging


def emit(p4, message):
    logging.getLogger("bt_delta.discovery").info(message)
    progress = getattr(p4, "progress", None)
    if progress:
        progress(message)
