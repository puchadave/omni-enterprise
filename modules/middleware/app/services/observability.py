import logging
from typing import Optional

from .error_codes import ErrorCode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def log_event(
    job_id: str,
    sku: str,
    error_code: Optional[ErrorCode] = None,
    status: Optional[str] = None,
    duration: Optional[float] = None,
):
    log_message = {
        "job_id": job_id,
        "sku": sku,
        "error_code": error_code.value if error_code else None,
        "status": status,
        "duration": duration,
    }
    logger.info(log_message)
