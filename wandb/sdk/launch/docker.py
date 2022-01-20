import json
import logging
import os
from platform import python_build, python_version
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence


from dockerpycreds.utils import find_executable  # type: ignore
import pkg_resources
from six.moves import shlex_quote
import wandb
from wandb.apis.internal import Api
import wandb.docker as docker
from wandb.errors import DockerError, ExecutionError, LaunchError
from wandb.util import get_module
from yaspin import yaspin  # type: ignore

from ._project_spec import (
    create_metadata_file,
    DEFAULT_LAUNCH_METADATA_PATH,
    EntryPoint,
    get_entry_point_command,
    LaunchProject,
)
from .utils import _is_wandb_dev_uri, _is_wandb_local_uri
from ..lib.git import GitRepo

_logger = logging.getLogger(__name__)

_GENERATED_DOCKERFILE_NAME = "Dockerfile.wandb-autogenerated"
API_KEY_REGEX = r"WANDB_API_KEY=\w+"


def validate_docker_installation() -> None:
    """Verify if Docker is installed on host machine."""
    if not find_executable("docker"):
        raise ExecutionError(
            "Could not find Docker executable. "
            "Ensure Docker is installed as per the instructions "
            "at https://docs.docker.com/install/overview/."
        )


def get_docker_user(launch_project):
    import getpass

    username = getpass.getuser()
    userid = launch_project.docker_user_id or os.geteuid()
    return username, userid


TEMPLATE = """
FROM python:{py_version_image} as build

# install in venv to copy
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN apt-get update -qq && apt-get install --no-install-recommends -y \
    {python_build_packages} \
    && apt-get -qq purge && apt-get -qq clean \
    && rm -rf /var/lib/apt/lists/*

COPY src/requirements.txt .
# different requirements line if we have buildx or not
{requirements_line}

# different base image for cpu/gpu
{base_setup}

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

ENV SHELL /bin/bash

# todo: handle uids
RUN useradd \
    --create-home \
    --no-log-init \
    --shell /bin/bash \
    --gid 0 \
    --uid {uid} \
    {user}

WORKDIR {workdir}
RUN chown {user} {workdir}

# add env vars
{env_vars}

# make artifacts cache dir unrelated to build
RUN mkdir -p {workdir}/.cache && chown -R {uid} {workdir}/.cache

# copy code/etc
COPY --chown={user} src/ {workdir}

# todo handle local installs

USER {user}

ENV PYTHONUNBUFFERED=1

ENTRYPOINT {command_arr}

"""


def get_current_python_version():
    full_version = sys.version.split()[0].split(".")
    major = full_version[0]
    version = ".".join(full_version[:2]) if len(full_version) >= 2 else major + ".0"
    return version, major


def generate_base_image_no_r2d(api, launch_project, image_uri, entry_cmd):
    if launch_project.python_version:
        py_version, py_major = (
            launch_project.python_version,
            launch_project.python_version.split(".")[0],
        )
    else:
        py_version, py_major = get_current_python_version()

    python_base_image = "{}-slim-buster".format(py_version)
    if launch_project.gpu:
        # must install all python setup
        if py_major == "2":
            python_packages = [
                "python{}".format(py_version),
                "python-pip",
                "python-setuptools",
            ]
        else:
            python_packages = [
                "python{}".format(py_version),
                "python3-pip",
                "python3-setuptools",
            ]

        base_setup = """
FROM nvidia/cuda:10.0-base as base
RUN apt-get update -qq && apt-get install -y software-properties-common && add-apt-repository -y ppa:deadsnakes/ppa

# install python
# todo support runtime.txt, setup.py
RUN apt-get update -qq && apt-get install --no-install-recommends -y \
    {python_packages} \
    && apt-get -qq purge && apt-get -qq clean \
    && rm -rf /var/lib/apt/lists/*

# make sure `python` points at the right version
RUN update-alternatives --install /usr/bin/python python /usr/bin/python{py_version} 1 \
    && update-alternatives --install /usr/local/bin/python python /usr/bin/python{py_version} 1
""".format(
            python_packages=" \\\n".join(python_packages), py_version=py_version
        )
    else:
        python_packages = [
            "python3-dev" if py_major == "3" else "python-dev",
            "gcc",
        ]  # required for python < 3.7

        base_setup = """
FROM python:{py_image} as base
""".format(
            py_image=python_base_image
        )

    username, userid = get_docker_user(launch_project)
    workdir = "/home/{user}".format(user=username)

    # add env vars
    if _is_wandb_local_uri(api.settings("base_url")) and sys.platform == "darwin":
        _, _, port = _, _, port = api.settings("base_url").split(":")
        base_url = "http://host.docker.internal:{}".format(port)
    elif _is_wandb_dev_uri(api.settings("base_url")):
        base_url = "http://host.docker.internal:9002"
    else:
        base_url = api.settings("base_url")
    env_vars_section = "\n".join(
        [
            f"ENV WANDB_BASE_URL={base_url}",
            f"ENV WANDB_API_KEY={api.api_key}",
            f"ENV WANDB_PROJECT={launch_project.target_project}",
            f"ENV WANDB_ENTITY={launch_project.target_entity}",
            f"ENV WANDB_LAUNCH={True}",
            f"ENV WANDB_LAUNCH_CONFIG_PATH={os.path.join(workdir, DEFAULT_LAUNCH_METADATA_PATH)}",
            f"ENV WANDB_RUN_ID={launch_project.run_id or None}",
            f"ENV WANDB_DOCKER={launch_project.docker_image}",
        ]
    )

    requirements_line = ""

    if docker.is_buildx_installed():
        requirements_line = "RUN --mount=type=cache,mode=0777,target={}/.cache,uid={},gid=0 ".format(  # todo: don't think this is working for partial caching
            workdir, launch_project.docker_user_id
        )
    else:
        wandb.termwarn(
            "Docker BuildX is not installed, for faster builds upgrade docker: https://github.com/docker/buildx#installing"
        )
        requirements_line = "RUN WANDB_DISABLE_CACHE=true "
    requirements_line += "pip install -r requirements.txt"

    python_build_packages = (
        ["python3-dev", "gcc"] if py_major == "3" else ["python-dev", "gcc"]
    )

    dockerfile_contents = TEMPLATE.format(
        py_version_image=python_base_image,
        user=username,
        uid=userid,
        env_vars=env_vars_section,
        workdir=workdir,
        command_arr=entry_cmd,
        requirements_line=requirements_line,
        base_setup=base_setup,
        python_build_packages=" ".join(python_build_packages),
    )
    print(dockerfile_contents)  # tmp

    build_ctx_path = _create_docker_build_ctx(launch_project, dockerfile_contents)
    dockerfile = os.path.join(build_ctx_path, _GENERATED_DOCKERFILE_NAME)

    try:
        image = docker.build(
            tags=[image_uri], file=dockerfile, context_path=build_ctx_path
        )
    except DockerError as e:
        raise LaunchError("Error communicating with docker client: {}".format(e))

    try:
        os.remove(build_ctx_path)
    except Exception:
        _logger.info(
            "Temporary docker context file %s was not deleted.", build_ctx_path
        )

    return image


def generate_docker_base_image(
    launch_project: LaunchProject, entry_cmd: str
) -> Optional[str]:
    """Uses project and entry point to generate the docker image."""
    path = launch_project.project_dir

    # this check will always pass since the dir attribute will always be populated
    # by _fetch_project_local
    _logger.info("Importing repo2docker...")
    get_module(
        "repo2docker",
        required='wandb launch requires additional dependencies, install with pip install "wandb[launch]"',
    )
    assert isinstance(path, str)
    _logger.info("Running repo2docker...")
    cmd: Sequence[str] = [
        "jupyter-repo2docker",
        "--no-run",
        "--image-name={}".format(launch_project.base_image),
        "--user-id={}".format(launch_project.docker_user_id),
        path,
        '"{}"'.format(entry_cmd),
    ]

    _logger.info(
        "Generating docker base image from git repo or finding image if it already exists..."
    )
    build_log = os.path.join(launch_project.project_dir, "build.log")
    _logger.info(f"Build log found at: {build_log}")
    with yaspin(
        text="Generating docker base image {}, this may take a few minutes...".format(
            launch_project.base_image
        )
    ).bouncingBar.blue as spinner:
        with open(build_log, "w") as f:
            process = subprocess.Popen(cmd, stdout=f, stderr=f)
            res = process.wait()
            if res == 0:
                spinner.text = "Generated docker base image {}".format(
                    launch_project.base_image
                )
                spinner.ok("✅ ")
            else:
                spinner.text = "Detailed error logs: {}".format(build_log)
                spinner.fail("💥 ")
                return None
    return launch_project.base_image


_inspected_images = {}


def docker_image_exists(docker_image: str, should_raise: bool = False) -> bool:
    """Checks if a specific image is already available,
    optionally raising an exception"""
    _logger.info("Checking if base image exists...")
    try:
        data = docker.run(["docker", "image", "inspect", docker_image])
        # always true, since return stderr defaults to false
        assert isinstance(data, str)
        parsed = json.loads(data)[0]
        _inspected_images[docker_image] = parsed
        _logger.info("Base image found. Won't generate new base image")
        return True
    except (DockerError, ValueError) as e:
        if should_raise:
            raise e
        _logger.info(
            "Base image not found. Generating new base image using repo2docker"
        )
        return False


def docker_image_inspect(docker_image: str) -> Dict[str, Any]:
    """Get the parsed json result of docker inspect image_name"""
    if _inspected_images.get(docker_image) is None:
        docker_image_exists(docker_image, True)
    return _inspected_images.get(docker_image, {})


def pull_docker_image(docker_image: str) -> None:
    """Pulls the requested docker image"""
    try:
        docker.run(["docker", "pull", docker_image])
    except DockerError as e:
        raise LaunchError("Docker server returned error: {}".format(e))


def construct_local_image_uri(launch_project: LaunchProject) -> str:
    image_uri = _get_docker_image_uri(
        name=launch_project.image_name,
        work_dir=launch_project.project_dir,
        image_id=launch_project.run_id,
    )
    return image_uri


def construct_gcp_image_uri(
    launch_project: LaunchProject, gcp_repo: str, gcp_project: str, gcp_registry: str,
) -> str:
    base_uri = construct_local_image_uri(launch_project)
    return "/".join([gcp_registry, gcp_project, gcp_repo, base_uri])


def build_docker_image_if_needed(
    launch_project: LaunchProject,
    api: Api,
    copy_code: bool,
    workdir: str,
    container_env: List[str],
    runner_type: str,
    image_uri: str,
    command_args: List[str],
) -> str:
    """
    Build a docker image containing the project in `work_dir`, using the base image.
    param launch_project: LaunchProject class instance
    :param api: instance of wandb.apis.internal Api
    :param copy_code: boolean indicating if code should be copied into the docker container
    """

    launch_project.docker_image = image_uri
    if docker_image_exists(image_uri) and not launch_project.build_image:
        wandb.termlog("Using existing image: {}".format(image_uri))
        return image_uri
    copy_code_line = ""
    requirements_line = ""
    # TODO: we currently assume the home directory holds the pip cache
    homedir = workdir
    # for custom base_images we attempt to introspect the homedir
    for env in container_env:
        if env.startswith("HOME="):
            homedir = env.split("=", 1)[1]
    if copy_code:
        copy_code_line = "COPY --chown={} ./src/ {}\n".format(
            launch_project.docker_user_id, workdir
        )
        if docker.is_buildx_installed():
            requirements_line = "RUN --mount=type=cache,target={}/.cache,uid={},gid=0 ".format(
                homedir, launch_project.docker_user_id
            )
        else:
            wandb.termwarn(
                "Docker BuildX is not installed, for faster builds upgrade docker: https://github.com/docker/buildx#installing"
            )
            requirements_line = "RUN WANDB_DISABLE_CACHE=true "
        shutil.copy(
            os.path.join(os.path.dirname(__file__), "templates", "_wandb_bootstrap.py"),
            os.path.join(launch_project.project_dir),
        )
        # TODO: make this configurable or change the default behavior...
        requirements_line += _parse_existing_requirements(launch_project)
        requirements_line += "python _wandb_bootstrap.py\n"

    name_line = ""
    if launch_project.name:
        name_line = "ENV WANDB_NAME={wandb_name}\n"
    dockerfile_contents = (
        "FROM {imagename}\n"
        # need to chown this directory for artifacts caching
        "RUN mkdir -p {homedir}/.cache && chown -R {uid} {homedir}/.cache\n"
        "{copy_code_line}"
        "{requirements_line}"
        "{name_line}"
    ).format(
        imagename=launch_project.base_image,
        uid=launch_project.docker_user_id,
        homedir=homedir,
        copy_code_line=copy_code_line,
        requirements_line=requirements_line,
        name_line=name_line,
    )

    # add env vars
    if _is_wandb_local_uri(api.settings("base_url")) and sys.platform == "darwin":
        _, _, port = _, _, port = api.settings("base_url").split(":")
        base_url = "http://host.docker.internal:{}".format(port)
    elif _is_wandb_dev_uri(api.settings("base_url")):
        base_url = "http://host.docker.internal:9002"
    else:
        base_url = api.settings("base_url")
    env_vars = "\n".join(
        [
            f"ENV WANDB_BASE_URL={base_url}",
            f"ENV WANDB_API_KEY={api.api_key}",
            f"ENV WANDB_PROJECT={launch_project.target_project}",
            f"ENV WANDB_ENTITY={launch_project.target_entity}",
            f"ENV WANDB_LAUNCH={True}",
            f"ENV WANDB_LAUNCH_CONFIG_PATH={os.path.join(workdir, DEFAULT_LAUNCH_METADATA_PATH)}",
            f"ENV WANDB_RUN_ID={launch_project.run_id or None}",
            f"ENV WANDB_DOCKER={launch_project.docker_image}",
        ]
    )
    dockerfile_contents += env_vars + "\n"

    sanitized_dockerfile_contents = re.sub(
        API_KEY_REGEX, "WANDB_API_KEY", dockerfile_contents
    )
    command_string = " ".join(command_args)
    sanitized_command_string = re.sub(API_KEY_REGEX, "WANDB_API_KEY", command_string)
    create_metadata_file(
        launch_project, sanitized_command_string, sanitized_dockerfile_contents
    )

    build_ctx_path = _create_docker_build_ctx(launch_project, dockerfile_contents)

    _logger.info("=== Building docker image %s ===", image_uri)

    dockerfile = os.path.join(build_ctx_path, _GENERATED_DOCKERFILE_NAME)
    wandb.termlog("Generating launch image {}".format(image_uri))
    try:
        image = docker.build(
            tags=[image_uri], file=dockerfile, context_path=build_ctx_path
        )
    except DockerError as e:
        raise LaunchError("Error communicating with docker client: {}".format(e))

    try:
        os.remove(build_ctx_path)
    except Exception:
        _logger.info(
            "Temporary docker context file %s was not deleted.", build_ctx_path
        )
    return image


def get_docker_command(image: str, docker_args: Dict[str, Any] = None,) -> List[str]:
    """Constructs the docker command using the image and docker args.

    Arguments:
    image: a Docker image to be run
    docker_args: a dictionary of additional docker args for the command
    """
    docker_path = "docker"
    cmd: List[Any] = [docker_path, "run", "--rm"]

    if docker_args:
        for name, value in docker_args.items():
            # Passed just the name as boolean flag
            if isinstance(value, bool) and value:
                if len(name) == 1:
                    cmd += ["-" + name]
                else:
                    cmd += ["--" + name]
            else:
                # Passed name=value
                if len(name) == 1:
                    cmd += ["-" + name, value]
                else:
                    cmd += ["--" + name, value]

    cmd += [image]
    return [shlex_quote(c) for c in cmd]


def _parse_existing_requirements(launch_project: LaunchProject) -> str:
    requirements_line = ""
    base_requirements = os.path.join(launch_project.project_dir, "requirements.txt")
    if os.path.exists(base_requirements):
        include_only = set()
        with open(base_requirements) as f:
            iter = pkg_resources.parse_requirements(f)
            while True:
                try:
                    pkg = next(iter)
                    if hasattr(pkg, "name"):
                        name = pkg.name.lower()  # type: ignore
                    else:
                        name = str(pkg)
                    include_only.add(shlex_quote(name))
                except StopIteration:
                    break
                # Different versions of pkg_resources throw different errors
                # just catch them all and ignore packages we can't parse
                except Exception as e:
                    _logger.warn(f"Unable to parse requirements.txt: {e}")
                    continue
        requirements_line += "WANDB_ONLY_INCLUDE={} ".format(",".join(include_only))
    return requirements_line


def _get_docker_image_uri(name: Optional[str], work_dir: str, image_id: str) -> str:
    """
    Returns an appropriate Docker image URI for a project based on the git hash of the specified
    working directory.
    :param name: The URI of the Docker repository with which to tag the image. The
                           repository URI is used as the prefix of the image URI.
    :param work_dir: Path to the working directory in which to search for a git commit hash
    """
    name = name.replace(" ", "-") if name else "wandb-launch"
    # Optionally include first 7 digits of git SHA in tag name, if available.

    git_commit = GitRepo(work_dir).last_commit
    version_string = ":" + str(git_commit[:7]) + image_id if git_commit else image_id
    return name + version_string


def _create_docker_build_ctx(
    launch_project: LaunchProject, dockerfile_contents: str,
) -> str:
    """Creates build context temp dir containing Dockerfile and project code, returning path to temp dir."""
    directory = tempfile.mkdtemp()
    dst_path = os.path.join(directory, "src")
    shutil.copytree(
        src=launch_project.project_dir, dst=dst_path, symlinks=True,
    )
    if launch_project.python_version:
        runtime_path = os.path.join(dst_path, "runtime.txt")
        with open(runtime_path, "w") as fp:
            fp.write(f"python-{launch_project.python_version}")
    # TODO: we likely don't need to pass the whole git repo into the container
    # with open(os.path.join(directory, ".dockerignore"), "w") as f:
    #    f.write("**/.git")
    with open(os.path.join(directory, _GENERATED_DOCKERFILE_NAME), "w") as handle:
        handle.write(dockerfile_contents)
    return directory


def get_full_command(
    image_uri: str,
    launch_project: LaunchProject,
    api: Api,
    container_workdir: str,
    docker_args: Dict[str, Any],
    entry_point: EntryPoint,
) -> List[str]:
    """Returns the full shell command to execute in order to run the specified entry point.

    Arguments:
    image_uri: image uri to run
    launch_project: LaunchProject instance used to construct the command
    api: Instance of wandb.apis.internal Api
    container_workdir: The working directory to use inside the container
    docker_args: Dictionary of docker args to pass to the container
    entry_point: Entry point to run

    Returns:
        List of strings representing the shell command to be executed
    """

    commands = []
    commands += get_docker_command(image_uri, docker_args)
    return commands
