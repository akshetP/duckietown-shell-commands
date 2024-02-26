import argparse
import sys
from enum import IntEnum
from threading import Thread
from time import sleep

import docker
import requests
from docker.errors import APIError, ContainerError
from docker.models.containers import Container

from dt_shell import DTCommandAbs, DTShell, dtslogger
from utils.cli_utils import ask_confirmation
from utils.docker_utils import get_client_OLD, get_endpoint_architecture, pull_image_OLD
from utils.misc_utils import sanitize_hostname
from utils.robot_utils import log_event_on_robot

UPGRADE_IMAGE = "duckietown/dt-firmware-upgrade:{distro}-{arch}"
HEALTH_CONTAINER_NAME = "device-health"
DEBUG = 0


class ExitCode(IntEnum):
    # NOTE: Please, DO NOT change these values, they are agreed upon with the image
    NOTHING_TO_DO = 255
    SUCCESS = 1
    HARDWARE_NOT_FOUND = 2
    HARDWARE_BUSY = 3
    HARDWARE_WRONG_MODE = 4
    FIRMWARE_UP_TO_DATE = 5
    FIRMWARE_NEEDS_UPDATE = 6
    GENERIC_ERROR = 9


ENV_KEY_PCB_VERSION = "PCB_VERSION"
PCB_VERSION_ID_EXIT_CODE_NONE = 0

# since the PCB version reading process does not return detailed error code,
# we allow only a number of trials, before aborting.
N_TRIALS_READ_PCB_VERSION = 3


class DTCommand(DTCommandAbs):
    help = "Upgrades a Duckiebot's battery firmware"

    @staticmethod
    def command(shell: DTShell, args):
        prog = "dts duckiebot battery upgrade DUCKIEBOT_NAME"
        parser = argparse.ArgumentParser(prog=prog)
        parser.add_argument("--force", action="store_true", default=False, help="Force the update")
        parser.add_argument("--version", type=str, default=None, help="Force a specific version")
        parser.add_argument("--debug", action="store_true", default=False, help="Debug mode")
        parser.add_argument("duckiebot", default=None, help="Name of the Duckiebot")
        parsed = parser.parse_args(args)
        # want the use NOT TO interrupt this command
        dtslogger.warning(
            "\n\nDO NOT unplug the battery, turn off your robot, or interrupt "
            "this command. It might cause irreversible damage to the battery.\n"
        )
        sleep(3)
        # check if the health-api container is running
        dtslogger.info("Releasing battery...")
        hostname = sanitize_hostname(parsed.duckiebot)
        client = get_client_OLD(hostname)
        device_health = None
        try:
            device_health = client.containers.get(HEALTH_CONTAINER_NAME)
        except docker.errors.NotFound:
            # this is fine
            pass
        except APIError as e:
            dtslogger.error(str(e))
            exit(1)
        # stop device health container
        if device_health is not None:
            device_health.reload()
            if device_health.status == "running":
                dtslogger.debug(f"Stopping '{HEALTH_CONTAINER_NAME}' container...")
                try:
                    device_health.stop()
                except APIError as e:
                    dtslogger.error(str(e))
                    exit(1)
                dtslogger.debug(f"Container '{HEALTH_CONTAINER_NAME}' stopped.")
            else:
                dtslogger.debug(f"Container '{HEALTH_CONTAINER_NAME}' not running.")
        else:
            dtslogger.debug(f"Container '{HEALTH_CONTAINER_NAME}' not found.")

        def start_container_and_try_blocking_until_healthy(
            _container: Container,
            msg_before: str = "Starting container...",
            msg_after: str = "Container started.",
            timeout_secs: int = 120,
        ):
            """
            Start a container.
            Try to wait until it's healthy. (If health status is enabled.)
            """

            dtslogger.info(msg_before)
            sleep(10)  # verified multiple times, this wait time is needed
            _container.start()

            def try_getting_health_status():
                try:
                    _container.reload()
                    return _container.attrs.get("State").get("Health").get("Status")
                except:
                    return None

            health_enabled = try_getting_health_status()
            if health_enabled is not None:
                dtslogger.info(
                    f'Waiting for container "{_container.name}" to become '
                    "healthy. It may take up to 2 minutes."
                )
                healthy = False
                secs = 0
                while not healthy and secs < timeout_secs:
                    dtslogger.debug(
                        f'Container "{_container.name}" not yet healthy. ' "Checking every second..."
                    )
                    try:
                        healthy = "healthy" == try_getting_health_status()
                        sleep(1)
                        secs += 1
                    except KeyboardInterrupt:
                        # force
                        dtslogger.debug("User gave up waiting for container to become healty.")
                        break

            dtslogger.info(msg_after)

        # the battery should be free now
        dtslogger.info("Battery released!")
        # compile upgrade image name
        arch = get_endpoint_architecture(hostname)
        distro: str = shell.profile.distro.name
        image = UPGRADE_IMAGE.format(distro=distro, arch=arch)
        dtslogger.info("Checking battery...")
        dtslogger.debug(f"Running image '{image}'")
        extra_env = {}

        # forcing a version means forcing an update (aka skipping the check)
        if parsed.version is not None:
            extra_env = {"FORCE_BATTERY_FW_VERSION": parsed.version}
            parsed.force = True

        # Always try to pull the latest dt-firmware-upgrade image
        dtslogger.info(f'Pulling image "{image}" on: {hostname}')
        try:
            pull_image_OLD(image, endpoint=client)
        except KeyboardInterrupt:
            dtslogger.info("Aborting.")
            return
        except Exception as e:
            dtslogger.error(f'An error occurred while pulling the image "{image}": {str(e)}')
            exit(1)
        dtslogger.info(f'The image "{image}" is now up-to-date.')

        # step 0: check PCB version of the board
        dtslogger.info(f"Fetching PCB version...")
        # we run the helper in "--find-pcbid" mode and expect one of:
        #   - 0 (PCB_VERSION_ID_EXIT_CODE_NONE) if any error occurred
        #   - any other int                     the obtained PCB version
        pcb_version = None
        for _ in range(N_TRIALS_READ_PCB_VERSION):
            exit_code = None
            logs = None
            try:
                container = client.containers.run(
                    image=image,
                    name="dts-battery-firmware-upgrade-find-pcbid",
                    privileged=True,
                    detach=True,
                    environment={"DEBUG": DEBUG},
                    command=["--", "--battery", "--find-pcbid"],
                )
                try:
                    data = container.wait(timeout=10)
                    exit_code, logs = data["StatusCode"], container.logs().decode("utf-8")
                except requests.exceptions.ReadTimeout:
                    container.stop()
                finally:
                    container.remove()
                if logs:
                    print(logs)
            except APIError as e:
                dtslogger.error(str(e))
                exit(1)

            if exit_code != PCB_VERSION_ID_EXIT_CODE_NONE:
                # valid result
                pcb_version = exit_code
                dtslogger.info(
                    f"[Success] The battery PCB version is: v{pcb_version}"
                )
                break
            # no valid PCB version read
            else:
                answer = input("Press ENTER to retry, 'q' to quit... ")
                if answer.strip() == "q":
                    exit(0)
                continue
        # did not manage to read PCB version in N_TRIALS_READ_PCB_VERSION times
        # abort operation and re-engage device_health
        if pcb_version is None:
            dtslogger.error((
                "Problem reading PCB version. "
                "Please save a copy of all above logs and contact your administrator."
            ))
            # re-activate device-health
            if device_health:
                start_container_and_try_blocking_until_healthy(
                    _container=device_health,
                    msg_before="Re-engaging battery (this might take a while)...",
                    msg_after="Battery returned to work!",
                )
            exit(1)
        # From this point on, the battery PCB version is stored in pcb_version

        # step 1. read the battery current version (unless forced)
        if not parsed.force:
            dtslogger.info(f"Checking for available battery firmware updates...")
            # we run the helper in "check" mode and expect one of:
            #   - FIRMWARE_UP_TO_DATE           user will be notified
            #   - FIRMWARE_NEEDS_UPDATE         all well, user will be asked to confirm
            while True:
                exit_code = None
                logs = None
                try:
                    check = client.containers.run(
                        image=image,
                        name="dts-battery-firmware-upgrade-check",
                        privileged=True,
                        detach=True,
                        environment={"DEBUG": DEBUG, ENV_KEY_PCB_VERSION: pcb_version},
                        command=["--", "--battery", "--check"],
                    )
                    try:
                        data = check.wait(timeout=10)
                        exit_code, logs = data["StatusCode"], check.logs().decode("utf-8")
                    except requests.exceptions.ReadTimeout:
                        check.stop()
                    finally:
                        check.remove()
                    if logs:
                        print(logs)
                except APIError as e:
                    dtslogger.error(str(e))
                    exit(1)
                # make sure we know what happened
                status = None
                # noinspection PyBroadException
                try:
                    status = ExitCode(exit_code)
                except BaseException:
                    dtslogger.error(
                        f"Unrecognized status code: {exit_code}.\n" f"Contact your administrator."
                    )
                    exit(1)
                # ---
                # FIRMWARE_UP_TO_DATE
                if status == ExitCode.FIRMWARE_UP_TO_DATE:
                    dtslogger.info(
                        f"The battery on {parsed.duckiebot} does not need to be"
                        f" updated. Enjoy the rest of your day."
                    )
                    # re-activate device-health
                    if device_health:
                        start_container_and_try_blocking_until_healthy(
                            _container=device_health,
                            msg_before="Re-engaging battery (this might take a while)...",
                            msg_after="Battery returned to work!",
                        )
                    exit(0)
                #
                elif status == ExitCode.FIRMWARE_NEEDS_UPDATE:
                    granted = ask_confirmation(
                        "An updated firmware is available", question="Do you want to update the battery now?"
                    )
                    if not granted:
                        dtslogger.info("Enjoy the rest of your day.")
                        exit(0)
                    break
                # any other status
                else:
                    answer = input("Press ENTER to retry, 'q' to quit... ")
                    if answer.strip() == "q":
                        exit(0)
                    continue

        # step 2: make sure everything is ready for update
        dtslogger.info('Switch your battery to "Boot Mode" by double pressing the button on the ' "battery.")
        # we run the helper in "dryrun" mode and expect:
        #   - SUCCESS           all well, next is update
        txt = "when done"
        container = None
        while True:
            answer = input(f"Press ENTER {txt}, 'q' to quit... ")
            if answer.strip() == "q":
                # reset battery, start device_health container
                dtslogger.warning('Set battery to "Normal Mode" by pressing the button ONCE on the battery.')
                sleep(1)
                input("Press ENTER when done...")
                # re-activate device-health
                if device_health:
                    start_container_and_try_blocking_until_healthy(
                        _container=device_health,
                        msg_before="Re-engaging battery (this might take a while)...",
                        msg_after="Battery returned to work!",
                    )
                exit(0)
            dtslogger.info('Checking if the battery has "Boot Mode" activated, please wait...')
            try:
                container = client.containers.run(
                    image=image,
                    name="dts-battery-firmware-upgrade-dryrun",
                    privileged=True,
                    environment={"DEBUG": DEBUG, ENV_KEY_PCB_VERSION: pcb_version, **extra_env},
                    command=["--", "--battery", "--dry-run"],
                )
            except APIError as e:
                dtslogger.error(str(e))
                exit(1)
            except ContainerError as e:
                if container:
                    try:
                        dtslogger.debug("Removing container 'dts-battery-firmware-upgrade-dryrun'...")
                        container.remove()
                        container = None
                    except APIError as e1:
                        dtslogger.error(str(e1))
                        exit(1)

                exit_code = e.exit_status
                # make sure we know what happened
                status = None
                # noinspection PyBroadException
                try:
                    status = ExitCode(exit_code)
                except BaseException:
                    dtslogger.error(f"Unrecognized status code: {exit_code}.\n Contact your administrator.")
                    exit(1)
                # SUCCESS
                if status == ExitCode.SUCCESS:
                    break
                # HARDWARE_WRONG_MODE
                elif status == ExitCode.HARDWARE_WRONG_MODE:
                    # battery found but not in boot mode
                    dtslogger.error(
                        "Battery detected in 'Normal Mode', but it needs to be in "
                        "'Boot Mode'. You can switch mode by pressing the button "
                        "on the battery twice."
                    )
                    txt = "to retry"
                    continue
                # HARDWARE_BUSY
                elif status == ExitCode.HARDWARE_BUSY:
                    # battery is busy
                    dtslogger.error(
                        "Battery detected but another process is using it. "
                        "This should not have happened. Contact your administrator."
                    )
                    exit(1)
                # any other status
                else:
                    dtslogger.error(f"The battery reported the status '{status.name}'")
                    exit(1)

        # step 3: perform update
        # it looks like the update is going to happen, mark the event
        log_event_on_robot(hostname, "battery/upgrade")
        dtslogger.info("Updating battery...")
        # we run the helper in "normal" mode and expect:
        #   - SUCCESS           all well, battery updated successfully
        container = None
        exit_code = None
        try:
            container = client.containers.run(
                image=image,
                name="dts-battery-firmware-upgrade-do",
                privileged=True,
                detach=True,
                environment={"DEBUG": DEBUG, ENV_KEY_PCB_VERSION: pcb_version, **extra_env},
                command=["--", "--battery"],
            )
            DTCommand._consume_output(container.attach(stream=True))
            data = container.wait(condition="stopped")
            exit_code = data["StatusCode"]
        except APIError as e:
            dtslogger.error(str(e))
            exit(1)
        # try cleaning up
        if container:
            try:
                dtslogger.debug("Removing container 'dts-battery-firmware-upgrade-do'...")
                container.remove()
            except APIError as e:
                dtslogger.error(str(e))
                exit(1)

        # make sure we know what happened
        status = None
        # noinspection PyBroadException
        try:
            status = ExitCode(exit_code)
        except BaseException:
            dtslogger.error(f"Unrecognized status code: {exit_code}.\n" f"Contact your administrator.")
            exit(1)
        # SUCCESS
        if status == ExitCode.SUCCESS:
            dtslogger.info(f"Battery on '{parsed.duckiebot}' successfully updated!")
        # any other status
        else:
            dtslogger.error(f"The battery reported the status '{status.name}'")
            exit(1)

        # re-activate device-health
        if device_health:
            start_container_and_try_blocking_until_healthy(
                _container=device_health,
                msg_before="Re-engaging battery (this might take a while)...",
                msg_after="Battery returned to work happier than ever!",
            )

    @staticmethod
    def _consume_output(logs):
        def _printer():
            for line in logs:
                sys.stdout.write(line.decode("utf-8"))

        worker = Thread(target=_printer())
        worker.start()
