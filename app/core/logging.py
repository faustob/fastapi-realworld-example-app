import logging
import sys
from types import FrameType
from typing import cast

from loguru import logger
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
    OTLPLogExporter,
)
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

from app.core import config

# Reuse the same OTel resource/endpoint configuration as the existing
# trace/metric SDK bootstrap. This LoggerProvider is registered here
# (the sole OTel bootstrap point for this service) and the resulting
# handler is attached to the root stdlib logger so records propagated
# from loguru (via the existing InterceptHandler reverse-bridge below)
# are exported with trace/span correlation from the active context.
_logger_provider = LoggerProvider()
set_logger_provider(_logger_provider)
_logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter()),
)
_otel_logging_handler = LoggingHandler(
    level=logging.NOTSET,
    logger_provider=_logger_provider,
)

# Attach the OTel handler to the root stdlib logger. loguru keeps its
# own sinks (stdout/console) untouched; this adds a propagation sink so
# loguru records also flow through stdlib logging where the OTel
# handler picks them up.
logging.getLogger().addHandler(_otel_logging_handler)
logger.add(
    logging.getLogger().handlers[-1].handle,
    format="{message}",
)


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = str(record.levelno)

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:  # noqa: WPS609
            frame = cast(FrameType, frame.f_back)
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level,
            record.getMessage(),
        )
