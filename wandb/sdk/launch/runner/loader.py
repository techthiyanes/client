import logging
from typing import Any, Dict

from wandb.apis.internal import Api
from wandb.errors import LaunchError
from wandb.sdk.launch.runner.abstract import AbstractRunner
from wandb.sdk.launch.runner.gcp_vertex import VertexRunner

from .local import LocalRunner

__logger__ = logging.getLogger(__name__)


# Statically register backend defined in wandb
WANDB_RUNNERS = {"local": LocalRunner, "gcp-vertex": VertexRunner}


def load_backend(
    backend_name: str, api: Api, backend_config: Dict[str, Any]
) -> AbstractRunner:
    # Static backends
    if backend_name in WANDB_RUNNERS:
        return WANDB_RUNNERS[backend_name](api, backend_config)

    raise LaunchError(
        "Resource name not among available resources. Available resources: {} ".format(
            ",".join(list(WANDB_RUNNERS.keys()))
        )
    )
