import os
import json
import subprocess
from typing import Any, Dict, List, Optional
import datetime
import yaml
import time

from six.moves import shlex_quote

import wandb
from wandb.sdk.launch.docker import validate_docker_installation
from wandb.errors import CommError, LaunchError
from wandb.apis.internal import Api

from google.cloud import aiplatform

from .._project_spec import (
    get_entry_point_command,
    LaunchProject,
)
from ..docker import (
    build_docker_image_if_needed,
    construct_gcp_image_uri,
    docker_image_exists,
    docker_image_inspect,
    generate_docker_base_image,
    pull_docker_image,
    validate_docker_installation,
)
from .abstract import AbstractRun, AbstractRunner, Status
from ..utils import (
    PROJECT_DOCKER_ARGS,
    PROJECT_SYNCHRONOUS,
)

GCP_CONSOLE_URI = "https://console.cloud.google.com"

class VertexSubmittedRun(AbstractRun):
    def __init__(self, job) -> None:
        self._job = job

    @property
    def id(self) -> int:
        return self._job.name   # numeric ID of the custom training job

    @property
    def name(self) -> str:
        return self._job.display_name

    @property
    def gcp_region(self) -> str:
        return self._job.location

    @property
    def gcp_project(self) -> str:
        return self._job.project

    def get_page_link(self) -> str:
        return "{console_uri}/vertex-ai/locations/{region}/training/{job_id}?project={project}".format(
            console_uri=GCP_CONSOLE_URI,
            region=self.gcp_region,
            job_id=self.id,
            project=self.gcp_project,
        )

    def wait(self) -> bool:
        self._job.wait()
        return self.get_status() == Status("finished")

    def get_status(self) -> Status:
        job_state = str(self._job.state)     # extract from type PipelineState
        if job_state == 'PipelineState.PIPELINE_STATE_SUCCEEDED':
            return Status("finished")
        if job_state == 'PipelineState.PIPELINE_STATE_FAILED':
            return Status("failed")
        if job_state == 'PipelineState.PIPELINE_STATE_RUNNING':
            return Status("running")
        return Status("unknown")

    def cancel(self) -> None:
        self._job.cancel()


class VertexRunner(AbstractRunner):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def run(self, launch_project: LaunchProject) -> Optional[AbstractRun]:
        resource_args = launch_project.resource_args
        gcp_config = get_gcp_config(resource_args.get("gcp_config") or "default")
        gcp_project = (
            resource_args.get("gcp_project")
            or gcp_config["properties"]["core"]["project"]
        )
        gcp_zone = resource_args.get("gcp_region") or gcp_config["properties"].get(
            "compute", {}
        ).get("zone")
        gcp_region = "-".join(gcp_zone.split("-")[:2])
        if not gcp_region:
            raise LaunchError(
                "GCP region not set. You can specify a region with --resource-arg gcp_region=<region> or a config with --resource-arg gcp_config=<config name>, otherwise uses region from GCP default config."
            )
        gcp_staging_bucket = resource_args.get("gcp_staging_bucket")
        if not gcp_staging_bucket:
            raise LaunchError(
                "Vertex requires a staging bucket for training and dependency packages in the same region as compute. You can specify a bucket with --resource-arg gcp_staging_bucket=<bucket>."
            )
        gcp_artifact_repo = resource_args.get("gcp_artifact_repo")
        if not gcp_artifact_repo:
            raise LaunchError(
                "Vertex requires an Artifact Registry repository for the Docker image. You can specify a repo with --resource-arg gcp_artifact_repo=<repo>."
            )
        gcp_docker_host = resource_args.get(
            "gcp_docker_host"
        ) or "{region}-docker.pkg.dev".format(region=gcp_region)
        gcp_machine_type = resource_args.get("gcp_machine_type") or "n1-standard-4"
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        gcp_training_job_name = resource_args.get("gcp_job_name") or "{project}_{time}".format(project=launch_project.target_project, time=timestamp)

        aiplatform.init(
            project=gcp_project, location=gcp_region, staging_bucket=gcp_staging_bucket
        )

        validate_docker_installation()
        synchronous: bool = self.backend_config[
            PROJECT_SYNCHRONOUS
        ]
        docker_args: Dict[str, Any] = self.backend_config[PROJECT_DOCKER_ARGS]
        if docker_args:
            wandb.termwarn(
                "Docker args are not supported for GCP. Not using docker args"
            )

        entry_point = launch_project.get_single_entry_point()

        entry_cmd = entry_point.command
        copy_code = True
        if launch_project.docker_image:
            pull_docker_image(launch_project.docker_image)
            copy_code = False
        else:
            # TODO: potentially pull the base_image
            if not docker_image_exists(launch_project.base_image):
                if generate_docker_base_image(launch_project, entry_cmd) is None:
                    raise LaunchError("Unable to build base image")
            else:
                wandb.termlog(
                    "Using existing base image: {}".format(launch_project.base_image)
                )

        command_args = []
        command_args += get_entry_point_command(
            entry_point, launch_project.override_args
        )

        container_inspect = docker_image_inspect(launch_project.base_image)
        container_workdir = container_inspect["ContainerConfig"].get("WorkingDir", "/")
        container_env: List[str] = container_inspect["ContainerConfig"]["Env"]

        if launch_project.docker_image is None or launch_project.build_image:
            image_uri = construct_gcp_image_uri(
                launch_project, gcp_artifact_repo, gcp_project, gcp_docker_host,
            )
            image = build_docker_image_if_needed(
                launch_project=launch_project,
                api=self._api,
                copy_code=copy_code,
                workdir=container_workdir,
                container_env=container_env,
                runner_type="gcp-vertex",
                image_uri=image_uri,
                command_args=command_args,
            )
        else:
            image = launch_project.docker_image

        # push to artifact registry
        subprocess.run(
            ["docker", "push", image]
        )  # todo: when aws pr is merged, use docker python tooling

        if self.backend_config.get("runQueueItemId"):
            try:
                self._api.ack_run_queue_item(
                    self.backend_config["runQueueItemId"], launch_project.run_id
                )
            except CommError:
                wandb.termerror(
                    "Error acking run queue item. Item lease may have ended or another process may have acked it."
                )
                return None

        job = aiplatform.CustomContainerTrainingJob(
            display_name=gcp_training_job_name, container_uri=image,
        )
        submitted_run = VertexSubmittedRun(job)

        # todo: support gcp dataset?

        wandb.termlog("Running training job {name} on {compute}.".format(name=gcp_training_job_name, compute=gcp_machine_type))

        # when sync is True, vertex blocks the main thread on job completion. when False, vertex returns a Future
        # on this thread but continues to block the process on another thread. always set sync=False so we can get
        # the job info (dependent on job._gca_resource)
        model = job.run(
            machine_type=gcp_machine_type, accelerator_count=0, replica_count=1, sync=False   # @@@ accelerator
        )
        while job._gca_resource is None:
            # give time for the gcp job object to be created, this should only loop a couple times max
            time.sleep(1)

        wandb.termlog("View your job status and logs at {url}.".format(url=submitted_run.get_page_link()))

        # hacky: if user doesn't want blocking behavior, kill both main thread and the background thread. job continues
        # to run remotely. this obviously doesn't work if we need to do some sort of postprocessing after this run fn
        if not synchronous:
            os._exit(0)

        return submitted_run


def run_shell(args):
    return subprocess.run(args, stdout=subprocess.PIPE).stdout.decode("utf-8").strip()


def get_gcp_config(config="default"):
    return yaml.safe_load(
        run_shell(
            ["gcloud", "config", "configurations", "describe", shlex_quote(config)]
        )
    )