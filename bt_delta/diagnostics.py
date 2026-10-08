"""Send the same discovery evidence to the saved log and live GUI log."""
import logging


def emit(p4, message):
    logging.getLogger("bt_delta.discovery").info(message)
    progress = getattr(p4, "progress", None)
    if progress:
        progress(message)
