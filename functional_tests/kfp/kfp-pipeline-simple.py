import random

import wandb
from kubernetes.client.models import V1EnvVar
from wandb.integration.kfp import wandb_log

import kfp
import kfp.dsl as dsl
from kfp import components


def add_wandb_env_variables(op):
    WANDB_API_KEY = V1EnvVar(name="WANDB_API_KEY", value=wandb.api.api_key)
    WANDB_PROJECT = V1EnvVar(name="WANDB_PROJECT", value="wandb_kfp_integration_test")
    return op.add_env_variable(WANDB_API_KEY).add_env_variable(WANDB_PROJECT)


@wandb_log
def add(a: float, b: float) -> float:
    return a + b


add = components.create_component_from_func(add)


@dsl.pipeline(name="adding-pipeline")
def testing_pipeline(seed: int, a: float, b: float):
    conf = dsl.get_pipeline_conf()
    conf.add_op_transformer(add_wandb_env_variables)
    add_task = add(a, b)
    add_task2 = add(add_task.output, add_task.output)


client = kfp.Client()

seed = random.randint(0, 999999)
a, b = random.random(), random.random()

run = client.create_run_from_pipeline_func(
    testing_pipeline, arguments={"seed": seed, "a": a, "b": b},
)

run.wait_for_run_completion()
