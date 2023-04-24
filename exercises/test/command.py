import argparse
import getpass
import json
import os
import platform
import random
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, cast, Dict, List, Optional

import grp
import requests
from docker import DockerClient
from docker.errors import APIError, NotFound
from docker.models.containers import Container
from requests import ReadTimeout

from dt_shell import DTCommandAbs, DTShell, dtslogger, UserError
from dt_shell.env_checks import check_docker_environment
from duckietown_docker_utils import continuously_monitor
from utils.cli_utils import ensure_command_is_installed, start_command_in_subprocess
from utils.docker_utils import (
    get_endpoint_architecture,
    get_registry_to_use,
    get_remote_client,
    pull_if_not_exist,
    pull_image_OLD,
    remove_if_running,
)
from utils.exceptions import InvalidUserInput
from utils.exercises_utils import BASELINE_IMAGES
from utils.misc_utils import sanitize_hostname
from utils.networking_utils import get_duckiebot_ip
from utils.notebook_utils import convert_notebooks
from utils.pip_utils import import_or_install
from utils.yaml_utils import load_yaml

usage = """

## Basic usage
    This is an helper for the exercises.
    You must run this command inside an exercise folder.

    To know more on the `exercises` commands, use `dts exercises test -h`.

        $ dts exercises test --duckiebot_name [DUCKIEBOT_NAME]

"""

BRANCH = "ente"
DEFAULT_ARCH = "amd64"
ROSCORE_IMAGE = f"duckietown/dt-commons:{BRANCH}"
SIMULATOR_IMAGE = f"duckietown/challenge-aido_lf-simulator-gym:{BRANCH}-amd64"  # no arch
EXPERIMENT_MANAGER_IMAGE = f"duckietown/challenge-aido_lf-experiment_manager:{BRANCH}-amd64"
BRIDGE_IMAGE = f"duckietown/dt-duckiebot-fifos-bridge:{BRANCH}"
VNC_IMAGE = f"duckietown/dt-gui-tools:{BRANCH}-amd64"

DEFAULT_REMOTE_USER = "duckie"
AGENT_ROS_PORT = "11312"

ENV_LOGLEVEL = "LOGLEVEL"
PORT_VNC = 8087
PORT_MANAGER = 8090
ROSBAG_DIR = "/data/logs"


class DTCommand(DTCommandAbs):
    @staticmethod
    def command(shell: DTShell, args):
        prog = "dts exercise test"
        parser = argparse.ArgumentParser(prog=prog, usage=usage)

        # to clone the mooc repo
        import_or_install("gitpython", "git")

        # to convert the notebook into a python script
        import_or_install("nbformat", "nbformat")
        import_or_install("nbconvert", "nbconvert")

        class Levels(str, Enum):

            LEVEL_NONE = "none"
            LEVEL_DEBUG = "debug"
            LEVEL_INFO = "info"
            LEVEL_WARNING = "warning"
            LEVEL_ERROR = "error"

        # noinspection PyUnresolvedReferences
        allowed_levels = [e.value for e in Levels]

        class ContainerNames(str, Enum):
            NAME_AGENT = "agent"
            NAME_MANAGER = "manager"
            NAME_SIMULATOR = "simulator"
            NAME_BRIDGE = "bridge"
            NAME_VNC = "vnc"

        loglevels: Dict[ContainerNames, Levels] = {
            ContainerNames.NAME_AGENT: Levels.LEVEL_DEBUG,
            ContainerNames.NAME_MANAGER: Levels.LEVEL_NONE,
            ContainerNames.NAME_SIMULATOR: Levels.LEVEL_NONE,
            ContainerNames.NAME_BRIDGE: Levels.LEVEL_NONE,
            ContainerNames.NAME_VNC: Levels.LEVEL_NONE,
        }

        parser.add_argument(
            "--duckiebot_name",
            "-b",
            dest="duckiebot_name",
            default=None,
            help="Name of the Duckiebot on which to run the exercise",
        )

        parser.add_argument(
            "--sim",
            "-s",
            dest="sim",
            action="store_true",
            default=False,
            help="Should we run it in the simulator instead of the real robot?",
        )

        parser.add_argument(
            "--stop",
            dest="stop",
            action="store_true",
            default=False,
            help="just stop all the containers",
        )

        parser.add_argument(
            "--local",
            "-l",
            dest="local",
            action="store_true",
            default=False,
            help="Should we run the agent locally (i.e. on this machine)? Important Note: "
            + "this is not expected to work on MacOSX",
        )

        parser.add_argument(
            "--pull", dest="pull", action="store_true", default=False, help="Should we pull all of the images"
        )

        loglevels_friendly = " ".join(f"{k.value}:{v}" for k, v in loglevels.items())
        parser.add_argument(
            "--logs",
            dest="logs",
            action="append",
            default=[],
            help=f"""
            
            Use --logs NAME:LEVEL to set up levels.
                
            The container names and their defaults are [{loglevels_friendly}].
            
            
            The levels are {", ".join(allowed_levels)}.
            
            """,
        )

        parser.add_argument(
            "--interactive",
            "-i",
            dest="interactive",
            action="store_true",
            default=False,
            help="Will run the agent in interactive mode with the code mounted",
        )

        parser.add_argument(
            "--challenge",
            help="Run in the environment of this challenge.",
        )
        parser.add_argument(
            "--scenarios",
            type=str,
            help="Uses the scenarios in the given directory.",
        )

        parser.add_argument(
            "--step",
            help="Run this step of the challenge",
        )

        parser.add_argument("launcher", nargs="?", default=None, help="(Optional) Launcher to execute")

        parsed = parser.parse_args(args)

        for line in parsed.logs:
            if ":" not in line:
                msg = f"Malformed option --logs {line}"
                raise UserError(msg)
            name, _, level = line.partition(":")
            name = cast(ContainerNames, name.lower())
            level = cast(Levels, level.lower())
            if name not in loglevels:
                msg = f"Invalid container name {name!r}, I know {list(loglevels)}"
                raise UserError(msg)
            if level not in allowed_levels:
                msg = f"Invalid log level {level!r}: must be one of {list(allowed_levels)}"
                raise UserError(msg)

            loglevels[name] = level

        loglevels_friendly = " ".join(f"{k.value}:{v}" for k, v in loglevels.items())
        dtslogger.info(f"Log levels = {loglevels_friendly}")

        #
        #   get current working directory to check if it is an exercise directory
        #
        working_dir = os.getcwd()
        exercise_name = (os.path.basename(working_dir)).lower()
        dtslogger.info(f"Running exercise {exercise_name}")

        config_file = os.path.join(working_dir, "config.yaml")

        if not os.path.exists(config_file):
            msg = "You must run this command inside the exercise directory"
            raise InvalidUserInput(msg)

        config = load_yaml(config_file)
        env_dir = os.path.join(working_dir, "assets/setup/")

        if parsed.launcher is not None:
            config["agent_run_cmd"] = f"{parsed.launcher}.sh"

        try:
            agent_base_image0 = BASELINE_IMAGES[config["agent_base"]]
        except Exception as e:
            msg = (
                f"Check config.yaml. Unknown base image {config['agent_base']}. "
                f"Available base images are {BASELINE_IMAGES}"
            )
            raise Exception(msg) from e

        use_ros = bool(config.get("ros", True))
        the_challenge = parsed.challenge or config.get("challenge", None)
        the_step = parsed.step or config.get("step", None)
        log_dir = config.get("log_dir", None)

        dtslogger.debug(f"config : {config}")
        dtslogger.debug(f"use_ros: {use_ros}")

        # get the local docker client
        local_client = check_docker_environment()

        # Keep track of the container to monitor
        # (only detached containers)
        # we will stop if one crashes
        containers_to_monitor = []

        # let's do all the input checks

        duckiebot_name = parsed.duckiebot_name
        if duckiebot_name is None and not parsed.sim:
            msg = "You must specify a duckiebot_name or run in the simulator"
            raise InvalidUserInput(msg)

        if not parsed.local and parsed.sim:
            dtslogger.info("Note: Running locally since we are using simulator")
            parsed.local = True

        if not parsed.sim:
            duckiebot_ip = get_duckiebot_ip(duckiebot_name)
            duckiebot_client = get_remote_client(duckiebot_ip)
            duckiebot_hostname = sanitize_hostname(duckiebot_name)
        else:
            duckiebot_client = duckiebot_hostname = duckiebot_ip = None

        # done input checks

        # Convert all the notebooks listed in the config file to python scripts and
        # move them in the specified package in the exercise ws.
        # Copy fiels listed in the config.yaml into the target_dir
        if "files" in config:
            convert_notebooks(config["files"])

        if parsed.local:
            agent_client = local_client
            arch = DEFAULT_ARCH
        else:
            # let's set some things up to run on the Duckiebot
            ensure_command_is_installed("rsync")
            remote_base_path = f"{DEFAULT_REMOTE_USER}@{duckiebot_hostname}:/code/{exercise_name}"
            dtslogger.info(f"Syncing your local folder with {duckiebot_name}")
            rsync_cmd = "rsync -a "
            if "rsync_exclude" in config:
                for d in config["rsync_exclude"]:
                    rsync_cmd += f"--exclude {working_dir}/{d} "
            rsync_cmd += f"{working_dir}/* {remote_base_path}"
            dtslogger.info(f"rsync command: {rsync_cmd}")
            _run_cmd(rsync_cmd, shell=True)

            # arch
            arch = get_endpoint_architecture(duckiebot_hostname)
            agent_client = duckiebot_client

        REGISTRY = get_registry_to_use()

        def add_registry(x):
            if REGISTRY in x:
                raise
            return REGISTRY + "/" + x

        use_challenge = the_challenge is not None

        sim_spec: ImageRunSpec
        expman_spec: ImageRunSpec

        if use_challenge:
            token = shell.shell_config.token_dt1
            if token is None:
                raise UserError("please set token")
            images = get_challenge_images(challenge=the_challenge, step=the_step, token=token)
            sim_spec = images["simulator"]
            expman_spec = images["evaluator"]
        else:
            sim_env = load_yaml(os.path.join(env_dir, "sim_env.yaml"))
            sim_spec = ImageRunSpec(add_registry(SIMULATOR_IMAGE), environment=sim_env, ports=[])
            expman_env = load_yaml(os.path.join(env_dir, "exp_manager_env.yaml"))
            expman_spec = ImageRunSpec(add_registry(EXPERIMENT_MANAGER_IMAGE), expman_env, ports=[])
        # let's update the images based on arch
        ros_image = add_registry(f"{ROSCORE_IMAGE}-{arch}")
        agent_base_image = add_registry(f"{agent_base_image0}-{arch}")
        bridge_image = add_registry(f"{BRIDGE_IMAGE}-{arch}")

        # let's clean up any mess from last time
        # this is probably not needed anymore since we clean up everything on exit.
        prefix = f"ex-{exercise_name}-"
        sim_container_name = f"{prefix}challenge-aido_lf-simulator-gym"
        ros_container_name = f"{prefix}ros_core"
        vnc_container_name = f"{prefix}dt-gui-tools"
        exp_manager_container_name = f"{prefix}experiment-manager"
        agent_container_name = f"{prefix}agent"
        bridge_container_name = f"{prefix}dt-duckiebot-fifos-bridge"

        remove_if_running(agent_client, sim_container_name)
        remove_if_running(agent_client, ros_container_name)
        remove_if_running(local_client, vnc_container_name)  # vnc always local
        remove_if_running(agent_client, exp_manager_container_name)
        remove_if_running(agent_client, agent_container_name)
        remove_if_running(agent_client, bridge_container_name)
        try:
            d = agent_client.networks.prune()
            dtslogger.debug(f"Successfully removed network {d}")
        except Exception as e:
            dtslogger.warn(f"error removing network: {e}")

        try:
            d = agent_client.volumes.prune()
            dtslogger.debug(f"Successfully removed volume {d}")
        except Exception as e:
            dtslogger.warn(f"error removing volume: {e}")

        if parsed.stop:
            dtslogger.info("Only stopping the containers. Exiting.")
            return

        # done cleaning

        if not parsed.local:
            ros_env = {
                "ROS_MASTER_URI": f"http://{duckiebot_name}.local:{AGENT_ROS_PORT}",
            }
        else:
            ros_env = {
                "ROS_MASTER_URI": f"http://{ros_container_name}:{AGENT_ROS_PORT}",
            }
            if parsed.sim:
                ros_env["VEHICLE_NAME"] = "agent"
                ros_env["HOSTNAME"] = "agent"
            else:
                ros_env["VEHICLE_NAME"] = duckiebot_name
                ros_env["HOSTNAME"] = duckiebot_name

        # let's see if we should pull the images
        local_images = [expman_spec.image_name, sim_spec.image_name]
        agent_images = [bridge_image, ros_image, agent_base_image]

        # ALL the pulling is done here. Don't start anything until we now
        if parsed.pull:
            for image in local_images:
                dtslogger.info(f"Pulling {image}")
                pull_image_OLD(image, local_client)
            for image in agent_images:
                dtslogger.info(f"Pulling {image}")
                pull_image_OLD(image, agent_client)
        else:
            for image in local_images:
                pull_if_not_exist(local_client, image)
            for image in agent_images:
                pull_if_not_exist(agent_client, image)

        try:
            agent_network = agent_client.networks.create("agent-network", driver="bridge")
        except Exception as e:
            msg = "error creating network"
            raise Exception(msg) from e

        uid = os.getuid()
        username = getpass.getuser()
        t = f"/tmp/{username}/exercises-test/"
        # TODO: use date/time
        thisone = str(random.randint(0, 100000))
        tmpdir = os.path.join(t, thisone)
        os.makedirs(tmpdir)

        fifos_dir = os.path.join(tmpdir, "run-fifos")
        if os.path.exists(fifos_dir):
            shutil.rmtree(fifos_dir)
        os.makedirs(fifos_dir)
        challenges_dir = os.path.join(tmpdir, "run-challenges")

        if os.path.exists(challenges_dir):
            shutil.rmtree(challenges_dir)
        os.makedirs(challenges_dir)
        # note: you must create a file in the /challenges mount point
        # because otherwise the experiment manager will think that something is off.
        os.makedirs(os.path.join(challenges_dir, "challenge-solution-output"))
        os.makedirs(os.path.join(challenges_dir, "challenge-evaluation-output"))
        os.makedirs(os.path.join(challenges_dir, "challenge-description"))
        os.makedirs(os.path.join(challenges_dir, "tmp"))

        touch_one = os.path.join(challenges_dir, "not_empty.txt")
        with open(touch_one, "w") as f:
            f.write("not_empty")

        # os.sync()
        time.sleep(3)

        assets_challenges_dir = os.path.join(working_dir, "assets/setup/challenges")

        if os.path.exists(assets_challenges_dir):
            shutil.copytree(assets_challenges_dir, os.path.join(challenges_dir, "exercise-challenges"))

        dtslogger.info(f"Results will be stored in: {challenges_dir}")

        fifos_bind0 = {fifos_dir: {"bind": "/fifos", "mode": "rw"}}
        if parsed.local:
            agent_challenge_dir = challenges_dir
        else:
            agent_challenge_dir = os.path.join("/data/logs", thisone)

        challenge_bind0 = {
            agent_challenge_dir: {
                "bind": "/challenges",
                "mode": "rw",
                "propagation": "rshared",
            }
        }

        avahi_bind0 = {}

        agent_bind = {
            **challenge_bind0,
            **fifos_bind0,
        }
        sim_bind = {
            **fifos_bind0,
        }
        bridge_bind = {
            **challenge_bind0,
            **fifos_bind0,
        }

        experiment_manager_bind = {
            # fifos_volume.name: {"bind": "/fifos", "mode": "rw"},
            challenges_dir: {
                "bind": "/challenges",
                "mode": "rw",
                "propagation": "rshared",
            },
            "/tmp": {"bind": "/tmp", "mode": "rw"},
            **fifos_bind0,
        }

        if parsed.scenarios is not None:

            scenarios = os.path.join(working_dir, parsed.scenarios)

            if not os.path.exists(scenarios):
                msg = f"Scenario directory does not exist: {scenarios}"
                raise UserError(msg)

            if not os.path.isdir(scenarios):
                msg = f"Need a directory for --scenarios"
                raise UserError(msg)

            experiment_manager_bind[scenarios] = {
                "bind": "/scenarios",
                "mode": "rw",
                "propagation": "rshared",
            }

        # are we running on a mac?
        if "darwin" in platform.system().lower():
            running_on_mac = True
        else:
            running_on_mac = False  # if we aren't running on mac we're on Linux

        # Launch things one by one
        auto_remove = False
        if parsed.sim:
            # let's launch the simulator
            dtslogger.info(f"Running simulator {sim_container_name} from {sim_spec.image_name}")
            env = dict(sim_spec.environment)
            if loglevels[ContainerNames.NAME_SIMULATOR] != Levels.LEVEL_NONE:
                env[ENV_LOGLEVEL] = loglevels[ContainerNames.NAME_SIMULATOR]
            env["USER"] = username
            env["UID"] = uid
            sim_params = {
                "image": sim_spec.image_name,
                "name": sim_container_name,
                "network": agent_network.name,  # always local
                "environment": env,
                "volumes": sim_bind,
                "auto_remove": auto_remove,
                "tty": True,
                "detach": True,
            }

            dtslogger.debug(sim_params)

            pull_if_not_exist(agent_client, sim_params["image"])
            sim_container = agent_client.containers.run(**sim_params)

            if loglevels[ContainerNames.NAME_SIMULATOR] != Levels.LEVEL_NONE:
                t = threading.Thread(target=continuously_monitor, args=(agent_client, sim_container_name))
                t.start()

            # let's launch the experiment_manager
            dtslogger.info(
                f"Running experiment_manager {exp_manager_container_name} " f"from {expman_spec.image_name}"
            )

            expman_env = dict(expman_spec.environment)
            if loglevels[ContainerNames.NAME_MANAGER] != Levels.LEVEL_NONE:
                expman_env[ENV_LOGLEVEL] = loglevels[ContainerNames.NAME_MANAGER]
            expman_env["USER"] = username
            expman_env["UID"] = uid
            expman_env["submitter_name"] = username
            expman_env["submission_id"] = "0"
            expman_env["challenge_name"] = exercise_name

            if use_challenge:
                if expman_spec.ports:
                    the_port = expman_spec.ports[0]
                    expman_port = {f"{the_port}/tcp": ("0.0.0.0", PORT_MANAGER)}
                else:
                    expman_port = {}
            else:
                expman_port = {"8090/tcp": ("0.0.0.0", PORT_MANAGER)}
            mw_params = {
                "image": expman_spec.image_name,
                "name": exp_manager_container_name,
                "environment": expman_env,
                "ports": expman_port,
                "network": agent_network.name,  # always local
                "volumes": experiment_manager_bind,
                "auto_remove": auto_remove,
                "detach": True,
                "tty": True,
                "user": uid,
            }

            dtslogger.debug(f"experiment_manager params = \n{json.dumps(mw_params, indent=2)}")

            # dtslogger.debug(mw_params)
            dtslogger.info(f"\n\tSim interface will be running at " f"http://localhost:{PORT_MANAGER}/\n")

            pull_if_not_exist(agent_client, mw_params["image"])
            mw_container = agent_client.containers.run(**mw_params)

            # add containers to monitor to the list (the order matters)
            containers_to_monitor.append(mw_container)
            containers_to_monitor.append(sim_container)

            if loglevels[ContainerNames.NAME_MANAGER] != Levels.LEVEL_NONE:
                t = threading.Thread(
                    target=continuously_monitor, args=(agent_client, exp_manager_container_name)
                )
                t.start()

        else:  # we are running on a duckiebot
            bridge_container = launch_bridge(
                bridge_container_name,
                env_dir,
                duckiebot_name,
                bridge_bind,
                bridge_image,
                parsed,
                running_on_mac,
                agent_client,
            )
            containers_to_monitor.append(bridge_container)

            if loglevels[ContainerNames.NAME_BRIDGE] != Levels.LEVEL_NONE:
                t = threading.Thread(target=continuously_monitor, args=(agent_client, bridge_container_name))
                t.start()

        # done with sim/duckiebot specific stuff.

        if use_ros:
            # let's launch the ros-core
            dtslogger.info(f"Running ROS container {ros_container_name} from {ros_image}")

            ros_port = {f"{AGENT_ROS_PORT}/tcp": ("0.0.0.0", AGENT_ROS_PORT)}

            if not running_on_mac:
                ros_volumes = {
                    "/var/run/avahi-daemon/socket": {
                        "bind": "/var/run/avahi-daemon/socket",
                        "mode": "rw",
                    }
                }
            else:
                ros_volumes = {}

            ros_params = {
                "image": ros_image,
                "name": ros_container_name,
                "environment": ros_env,
                "detach": True,
                "volumes": ros_volumes,
                "auto_remove": auto_remove,
                "tty": True,
                "command": f"roscore -p {AGENT_ROS_PORT}",
            }

            if parsed.local:
                ros_params["network"] = agent_network.name
                ros_params["ports"] = ros_port
            else:
                ros_params["network_mode"] = "host"

            dtslogger.debug(ros_params)
            pull_if_not_exist(agent_client, ros_params["image"])
            ros_container = agent_client.containers.run(**ros_params)
            containers_to_monitor.append(ros_container)

            # let's launch vnc
            # if we have a lab_dir - then let's see if the image exists locally otherwise try to build
            # otherwise just use the base image
            labdir_name = config.get("lab_dir", None)
            if labdir_name is None:
                dtslogger.info("No lab dir - running base VNC image")
                vnc_image = VNC_IMAGE
                vnc_image = add_registry(vnc_image)
            else:
                vnc_image = f"{getpass.getuser()}/exercise-{exercise_name}-lab:latest"
                try:
                    local_client_images = local_client.images.get(vnc_image)
                except Exception as e:
                    dtslogger.error(
                        f"Failed to find <Image: '{vnc_image}:latest'> in local images."
                        "You must run dts exercises build first to build your lab image to run "
                        "notebooks"
                    )

            dtslogger.info(f"Running VNC {vnc_container_name} from {vnc_image}")
            vnc_env = ros_env
            if not parsed.local:
                vnc_env["VEHICLE_NAME"] = duckiebot_name
                vnc_env["ROS_MASTER"] = duckiebot_name
                vnc_env["HOSTNAME"] = duckiebot_name

            vnc_volumes = {
                os.path.join(working_dir, "launchers"): {
                    "bind": "/code/launchers",
                    "mode": "ro",
                }
            }

            if log_dir is not None:
                vnc_volumes[os.path.join(working_dir, log_dir)] = {
                    "bind": ROSBAG_DIR,
                    "mode": "rw",
                }

            vnc_params = {
                "image": vnc_image,
                "name": vnc_container_name,
                "command": "dt-launcher-vnc",
                "environment": vnc_env,
                "volumes": vnc_volumes,
                "auto_remove": auto_remove,
                "privileged": True,
                "stream": True,
                "detach": True,
                "tty": True,
            }

            if not running_on_mac:
                vnc_params["volumes"]["/var/run/avahi-daemon/socket"] = {
                    "bind": "/var/run/avahi-daemon/socket",
                    "mode": "rw",
                }

            if parsed.local:
                vnc_params["network"] = agent_network.name
                vnc_params["ports"] = {"8087/tcp": ("0.0.0.0", PORT_VNC)}
            else:
                if not running_on_mac:
                    vnc_params["network_mode"] = "host"

                # vnc_params["ports"] = {"8087/tcp": ("0.0.0.0", PORT_VNC)}

            dtslogger.debug(f"vnc_params: {json.dumps(vnc_params, sort_keys=True, indent=4)}")

            # vnc always runs on local client
            vnc_container = local_client.containers.run(**vnc_params)
            containers_to_monitor.append(vnc_container)

            dtslogger.info(f"\n\tVNC running at http://localhost:{PORT_VNC}/\n")

            if loglevels[ContainerNames.NAME_VNC] != Levels.LEVEL_NONE:
                t = threading.Thread(target=continuously_monitor, args=(local_client, vnc_container_name))
                t.start()

        # Setup functions for monitor and cleanup
        def stop_attached_container():
            container = agent_client.containers.get(agent_container_name)
            container.reload()
            if container.status == "running":
                container.kill(signal.SIGINT)

        containers_monitor = launch_container_monitor(containers_to_monitor, stop_attached_container)

        # We will catch CTRL+C and cleanup containers
        signal.signal(
            signal.SIGINT,
            lambda signum, frame: clean_shutdown(
                containers_monitor, containers_to_monitor, stop_attached_container
            ),
        )

        dtslogger.info("Starting attached container")

        agent_env = load_yaml(os.path.join(env_dir, "agent_env.yaml"))
        if use_ros:
            agent_env = {**ros_env, **agent_env}

        if loglevels[ContainerNames.NAME_AGENT] != Levels.LEVEL_NONE:
            agent_env[ENV_LOGLEVEL] = loglevels[ContainerNames.NAME_AGENT]

        try:
            launch_agent(
                agent_container_name=agent_container_name,
                agent_volumes=agent_bind,
                parsed=parsed,
                working_dir=working_dir,
                exercise_name=exercise_name,
                agent_base_image=agent_base_image,
                agent_network=agent_network,
                agent_client=agent_client,
                duckiebot_name=duckiebot_name,
                config=config,
                agent_env=agent_env,
            )
        except Exception as e:
            dtslogger.error(f"Attached container terminated {e}")
        finally:
            clean_shutdown(containers_monitor, containers_to_monitor, stop_attached_container)

        dtslogger.info(f"All done, your results are available in: {challenges_dir}")


def clean_shutdown(
    containers_monitor: "ContainersMonitor",
    containers: List[Container],
    stop_attached_container: Callable[[], None],
):
    dtslogger.info("Stopping container monitor...")
    containers_monitor.shutdown()
    while containers_monitor.is_alive():
        time.sleep(1)
    dtslogger.info("Container monitor stopped.")
    # ---
    dtslogger.info("Cleaning containers...")
    for container in containers:
        dtslogger.info(f"Stopping container {container.name}")
        try:
            container.stop()
        except NotFound:
            # all is well
            pass
        except APIError as e:
            dtslogger.info(f"Container {container.name} already stopped ({str(e)})")
    for container in containers:
        dtslogger.info(f"Waiting for container {container.name} to stop...")
        try:
            container.wait()
        except (NotFound, APIError, ReadTimeout):
            # all is well
            pass
    # noinspection PyBroadException
    try:
        stop_attached_container()
    except BaseException:
        dtslogger.info(f"attached container already stopped.")


def launch_container_monitor(
    containers_to_monitor: List[Container], stop_attached_container: Callable[[], None]
) -> "ContainersMonitor":
    """
    Start a daemon thread that will exit when the application exits.
    Monitor should stop everything if a containers exits and display logs.
    """
    monitor_thread = ContainersMonitor(containers_to_monitor, stop_attached_container)
    dtslogger.info("Starting monitor thread")
    dtslogger.info(f"Containers to monitor: {list(map(lambda c: c.name, containers_to_monitor))}")
    monitor_thread.start()
    return monitor_thread


class ContainersMonitor(threading.Thread):
    def __init__(self, containers_to_monitor: List[Container], stop_attached_container: Callable[[], None]):
        super().__init__(daemon=True)
        self._containers_to_monitor = containers_to_monitor
        self._stop_attached_container = stop_attached_container
        self._is_shutdown = False

    def shutdown(self):
        self._is_shutdown = True

    def run(self):
        """
        When an error is found, we display info and kill the attached thread to stop main process.
        """
        counter = -1
        check_every_secs = 5
        while not self._is_shutdown:
            counter += 1
            if counter % check_every_secs != 0:
                time.sleep(1)
                continue
            # ---
            errors = []
            dtslogger.debug(f"{len(self._containers_to_monitor)} container to monitor")
            for container in self._containers_to_monitor:
                try:
                    container.reload()
                except (APIError, TimeoutError) as e:
                    dtslogger.warn(f"Cannot reload container {container.name!r}: {e}")
                    continue
                status = container.status
                dtslogger.debug(f"container {container.name} in state {status}")
                if status in ["exited", "dead"]:
                    errors.append(
                        {
                            "name": container.name,
                            "id": container.id,
                            "status": container.status,
                            "image": container.image.attrs["RepoTags"],
                            "logs": container.logs(),
                        }
                    )
                else:
                    dtslogger.debug("Containers monitor check passed.")

            if errors:
                dtslogger.info(f"Monitor found {len(errors)} exited containers")
                for e in errors:
                    dtslogger.error(
                        f"""Monitored container exited:
                    container: {e['name']}
                    id: {e['id']}
                    status: {e['status']}
                    image: {e['image']}
                    logs: {e['logs'].decode()}
                    """
                    )
                dtslogger.info("Sending kill to container attached container")
                self._stop_attached_container()
            # sleep
            time.sleep(1)


def launch_agent(
    agent_container_name: str,
    agent_volumes,
    parsed,
    working_dir: str,
    exercise_name: str,
    agent_base_image: str,
    agent_network,
    agent_client: DockerClient,
    duckiebot_name: str,
    config,
    agent_env: Dict[str, str],
):
    # Let's launch the ros template
    dtslogger.info(f"Running the {agent_container_name} from {agent_base_image}")

    ws_dir = "/" + config["ws_dir"]

    if parsed.sim or parsed.local:
        agent_volumes[working_dir + "/assets"] = {"bind": "/data/config", "mode": "rw"}
        agent_volumes[working_dir + "/launchers"] = {"bind": "/code/launchers", "mode": "rw"}
        agent_volumes[working_dir + ws_dir] = {"bind": f"/code{ws_dir}", "mode": "rw"}
    else:
        agent_volumes[f"/data/config"] = {"bind": "/data/config", "mode": "rw"}
        agent_volumes[f"/code/{exercise_name}/launchers"] = {"bind": "/code/launchers", "mode": "rw"}
        agent_volumes[f"/code/{exercise_name}{ws_dir}"] = {
            "bind": f"/code{ws_dir}",
            "mode": "rw",
        }

    if parsed.local and not parsed.sim:
        # get the calibrations from the robot with the REST API
        get_calibration_files(working_dir + "/assets", parsed.duckiebot_name)

    on_mac = "Darwin" in platform.system()
    if on_mac:
        group_add = []
    else:
        group_add = [g.gr_gid for g in grp.getgrall() if getpass.getuser() in g.gr_mem]

    agent_env["PYTHONDONTWRITEBYTECODE"] = "1"
    agent_params = {
        "image": agent_base_image,
        "name": agent_container_name,
        "volumes": agent_volumes,
        "environment": agent_env,
        "auto_remove": True,
        "detach": True,
        "tty": True,
        "group_add": group_add,
        "command": [f"/code/launchers/{config['agent_run_cmd']}"],
    }

    if parsed.local:
        agent_params["network"] = agent_network.name
    else:
        agent_params["network_mode"] = "host"

    if parsed.interactive:
        agent_params["command"] = "/bin/bash"
        agent_params["stdin_open"] = True

    dtslogger.debug(agent_params)

    if not on_mac:
        agent_params["volumes"]["/var/run/avahi-daemon/socket"] = {
            "bind": "/var/run/avahi-daemon/socket",
            "mode": "rw",
        }

    pull_if_not_exist(agent_client, agent_params["image"])
    agent_container = agent_client.containers.run(**agent_params)

    attach_cmd = "docker %s attach %s" % (
        "" if parsed.local else f"-H {duckiebot_name}.local",
        agent_container_name,
    )
    start_command_in_subprocess(attach_cmd)

    return agent_container


def launch_bridge(
    bridge_container_name,
    env_dir,
    duckiebot_name,
    fifos_bind,
    bridge_image,
    parsed,
    running_on_mac,
    agent_client,
):
    # let's launch the duckiebot fifos bridge, note that this one runs in a different
    # ROS environment, the one on the robot

    dtslogger.info(f"Running {bridge_container_name} from {bridge_image}")
    bridge_env = {
        "HOSTNAME": f"{duckiebot_name}",
        "VEHICLE_NAME": f"{duckiebot_name}",
        "ROS_MASTER_URI": f"http://{duckiebot_name}.local:11311",
        **load_yaml(env_dir + "duckiebot_bridge_env.yaml"),
    }
    bridge_volumes = fifos_bind
    if not running_on_mac or not parsed.local:
        bridge_volumes["/var/run/avahi-daemon/socket"] = {
            "bind": "/var/run/avahi-daemon/socket",
            "mode": "rw",
        }

    bridge_params = {
        "image": bridge_image,
        "name": bridge_container_name,
        "environment": bridge_env,
        "network_mode": "host",  # bridge always on host
        "volumes": fifos_bind,
        "detach": True,
        "tty": True,
    }

    # if we are local - we need to have a network so that the hostname
    # matches the ROS_MASTER_URI or else ROS complains. If we are running on the
    # Duckiebot we set the hostname to be the duckiebot name so we can use host mode
    if parsed.local and running_on_mac:
        dtslogger.warn(
            "WARNING: Running agent locally not in simulator is not expected to work. "
            "Suggest to remove the --local flag"
        )

    dtslogger.debug(bridge_params)

    pull_if_not_exist(agent_client, bridge_params["image"])
    bridge_container = agent_client.containers.run(**bridge_params)
    return bridge_container


def _run_cmd(cmd, get_output=False, print_output=False, suppress_errors=False, shell=False):
    if shell and isinstance(cmd, (list, tuple)):
        cmd = " ".join([str(s) for s in cmd])
    dtslogger.debug("$ %s" % cmd)
    if get_output:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=shell)
        proc.wait()
        if proc.returncode != 0:
            if not suppress_errors:
                msg = "The command {} returned exit code {}".format(cmd, proc.returncode)
                dtslogger.error(msg)
                raise RuntimeError(msg)
        out = proc.stdout.read().decode("utf-8").rstrip()
        if print_output:
            print(out)
        return out
    else:
        try:
            subprocess.check_call(cmd, shell=shell)
        except subprocess.CalledProcessError as e:
            if not suppress_errors:
                raise e


# get the calibration files off the robot
def get_calibration_files(destination_dir, duckiebot_name):
    dtslogger.info("Getting all calibration files")

    calib_files = [
        "calibrations/camera_intrinsic/{duckiebot:s}.yaml",
        "calibrations/camera_extrinsic/{duckiebot:s}.yaml",
        "calibrations/kinematics/{duckiebot:s}.yaml",
    ]

    for calib_file in calib_files:
        calib_file = calib_file.format(duckiebot=duckiebot_name)
        url = "http://{:s}.local/files/data/config/{:s}".format(duckiebot_name, calib_file)
        # get calibration using the files API
        dtslogger.debug('Fetching file "{:s}"'.format(url))
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            dtslogger.warn(
                "Could not get the calibration file {:s} from robot {:s}. Is it calibrated? "
                "".format(calib_file, duckiebot_name)
            )
            continue
        # make destination directory
        dirname = os.path.join(destination_dir, os.path.dirname(calib_file))
        if not os.path.isdir(dirname):
            dtslogger.debug('Creating directory "{:s}"'.format(dirname))
            os.makedirs(dirname)
        # save calibration file to disk
        # Also save them to specific robot name for local evaluation
        destination_file = os.path.join(dirname, f"{duckiebot_name}.yaml")
        dtslogger.debug(
            'Writing calibration file "{:s}:{:s}" to "{:s}"'.format(
                duckiebot_name, calib_file, destination_file
            )
        )
        with open(destination_file, "wb") as fd:
            for chunk in res.iter_content(chunk_size=128):
                fd.write(chunk)


@dataclass
class ImageRunSpec:
    image_name: str
    environment: Dict
    ports: List[str]


def get_challenge_images(challenge: str, step: Optional[str], token: str) -> Dict[str, ImageRunSpec]:
    default = "https://challenges.duckietown.org/v4"
    server = os.environ.get("DTSERVER", default)
    url = f"{server}/api/challenges/{challenge}/description"
    dtslogger.info(url)
    headers = {"X-Messaging-Token": token}
    res = requests.request("GET", url=url, headers=headers)
    if res.status_code == 404:
        msg = f"Cannot find challenge {challenge} on server; url = {url}"
        raise UserError(msg)
    j = res.json()
    dtslogger.debug(json.dumps(j, indent=1))
    if "result" not in j:
        msg = f"Cannot get data from server at url = {url}"
        raise Exception(msg)
    steps = j["result"]["challenge"]["steps"]
    step_names = list(steps)
    dtslogger.debug(f"steps are {step_names}")
    if step is None:
        step = step_names[0]
    else:
        if step not in step_names:
            msg = f"Wrong step name '{step}'; available {step_names}"
            raise UserError(msg)

    s = steps[step]
    services = s["evaluation_parameters"]["services"]
    res = {}
    for k, v in services.items():
        res[k] = ImageRunSpec(
            image_name=v["image"], environment=v.get("environment", {}), ports=v.get("ports", [])
        )
    return res
