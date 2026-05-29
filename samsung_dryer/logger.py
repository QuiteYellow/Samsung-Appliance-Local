"""Stdout logging — works correctly under Docker's PYTHONUNBUFFERED=1."""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-5s  %(name)s  %(message)s',
    datefmt='%H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("samsung_dryer")
