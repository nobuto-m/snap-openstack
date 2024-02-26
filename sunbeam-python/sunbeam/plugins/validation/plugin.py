# Copyright (c) 2023 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from croniter import croniter
from packaging.version import Version
from rich.console import Console

from sunbeam.clusterd.service import ClusterServiceUnavailableException
from sunbeam.commands.openstack import OPENSTACK_MODEL
from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.juju import JujuHelper, run_sync
from sunbeam.jobs.plugin import PluginManager
from sunbeam.plugins.interface.v1.openstack import (
    OpenStackControlPlanePlugin,
    TerraformPlanLocation,
)

LOG = logging.getLogger(__name__)
console = Console()

PLUGIN_VERSION = "0.0.1"
MINIMAL_PERIOD = 15 * 60  # 15 minutes in seconds
TEMPEST_APP_NAME = "tempest"
TEMPEST_CONTAINER_NAME = "tempest"
TEMPEST_CHANNEL = "2023.2/edge"
TEMPEST_VALIDATION_RESULT = "/var/lib/tempest/workspace/tempest-validation.log"
VALIDATION_PLUGIN_DEPLOY_TIMEOUT = (
    60 * 60
)  # 60 minutes in seconds, tempest can take some time to initialized


def validated_schedule(schedule: str) -> str:
    """Validate the schedule config option.

    Return the valid schedule if valid,
    otherwise Raise a click BadParameter exception.
    """
    # Empty schedule is fine; it means it's disabled in this context.
    if not schedule:
        return ""

    # croniter supports second repeats, but vixie cron does not.
    if len(schedule.split()) == 6:
        raise click.ClickException(
            "This cron does not support seconds in schedule (6 fields)."
            " Exactly 5 columns must be specified for iterator expression."
        )

    # constant base time for consistency
    base = datetime(2004, 3, 5)

    try:
        cron = croniter(schedule, base, max_years_between_matches=1)
    except ValueError as e:
        msg = str(e)
        # croniter supports second repeats, but vixie cron does not,
        # so update the error message here to suit.
        if "Exactly 5 or 6 columns" in msg:
            msg = "Exactly 5 columns must be specified for iterator expression."
        raise click.ClickException(msg)

    # This is a rather naive method for enforcing this,
    # and it may be possible to craft an expression
    # that results in some consecutive runs within 15 minutes,
    # however this is fine, as there is process locking for tempest,
    # and this is more of a sanity check than a security requirement.
    t1 = cron.get_next()
    t2 = cron.get_next()
    if t2 - t1 < MINIMAL_PERIOD:
        raise click.ClickException(
            "Cannot schedule periodic check to run faster than every 15 minutes."
        )

    return schedule


@dataclass()
class Config:
    """Represents config updates provided by the user.

    None values mean the user did not provide them.
    """

    schedule: Optional[str] = None


def parse_config_args(args: List[str]) -> Dict[str, str]:
    """Parse key=value args into a valid dictionary of key: values.

    Raise a click bad argument error if errors (only checks syntax here).
    """
    config = {}
    for arg in args:
        split_arg = arg.split("=", 1)
        if len(split_arg) == 1:
            raise click.ClickException("syntax: key=value")
        key, value = split_arg
        if key in config:
            raise click.ClickException(
                f"{key!r} parameter seen multiple times. Only provide it once."
            )
        config[key] = value
    return config


def validated_config_args(args: Dict[str, str]) -> Config:
    """Validate config and return validated config if no errors.

    Raise a click bad argument error if errors.
    """

    config = Config()

    for key, value in args.items():
        if key == "schedule":
            config.schedule = validated_schedule(value)
        else:
            raise click.ClickException(f"{key!r} is not a supported config option")

    return config


class ValidationPlugin(OpenStackControlPlanePlugin):
    """Deploy tempest to openstack model."""

    version = Version(PLUGIN_VERSION)

    def __init__(self, deployment: Deployment) -> None:
        """Initialize the plugin class."""
        super().__init__(
            "validation",
            deployment,
            tf_plan_location=TerraformPlanLocation.SUNBEAM_TERRAFORM_REPO,
        )

    def set_application_names(self) -> list:
        """Application names handled by the terraform plan."""
        return [TEMPEST_APP_NAME]

    def set_tfvars_on_enable(self) -> dict:
        """Set terraform variables to enable the application."""
        return {
            "enable-validation": True,
            "tempest-channel": TEMPEST_CHANNEL,
        }

    def set_application_timeout_on_enable(self) -> int:
        """Set Application Timeout on enabling the plugin.

        The plugin plan will timeout if the applications
        are not in active status within in this time.
        """
        return VALIDATION_PLUGIN_DEPLOY_TIMEOUT

    def set_application_timeout_on_disable(self) -> int:
        """Set Application Timeout on disabling the plugin.

        The plugin plan will timeout if the applications
        are not removed within this time.
        """
        return VALIDATION_PLUGIN_DEPLOY_TIMEOUT

    def set_tfvars_on_disable(self) -> dict:
        """Set terraform variables to disable the application."""
        return {"enable-validation": False}

    def set_tfvars_on_resize(self) -> dict:
        """Set terraform variables to resize the application."""
        return {}

    def _get_tempest_leader_unit(self) -> str:
        """Return the leader unit of tempest application."""
        jhelper = JujuHelper(self.deployment.get_connected_controller())
        with console.status(f"Retrieving {TEMPEST_APP_NAME}'s unit name."):
            app = TEMPEST_APP_NAME
            model = OPENSTACK_MODEL
            unit = run_sync(jhelper.get_leader_unit(app, model))
            if not unit:
                message = f"Unable to get {app} leader"
                raise click.ClickException(message)
            return unit

    def _run_action_on_tempest_unit(
        self,
        action_name: str,
        action_params: Optional[dict] = None,
        progress_message: str = "",
    ) -> Dict[str, Any]:
        """Run the charm's action."""
        unit = self._get_tempest_leader_unit()
        jhelper = JujuHelper(self.deployment.get_connected_controller())
        with console.status(progress_message):
            action_result = run_sync(
                jhelper.run_action(
                    unit,
                    OPENSTACK_MODEL,
                    action_name,
                    action_params or {},
                )
            )

            if action_result.get("return-code", 0) > 1:
                message = f"Unable to run action: {action_name}"
                raise click.ClickException(message)

            return action_result

    def _check_file_exist_in_tempest_container(self, filename: str) -> bool:
        """Check if file exist in tempest container."""
        unit = self._get_tempest_leader_unit()
        # Note: this is a workaround to run command to payload container
        # since python-libjuju does not support such feature. See related
        # bug: https://github.com/juju/python-libjuju/issues/1029
        result = subprocess.run(
            [
                "juju",
                "ssh",
                "--container",
                TEMPEST_CONTAINER_NAME,
                unit,
                "ls",
                TEMPEST_VALIDATION_RESULT,
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            return False
        return True

    def _copy_file_from_tempest_container(self, source: str, destination: str) -> None:
        """Copy file from tempest container."""
        unit = self._get_tempest_leader_unit()
        progress_message = (
            f"Copying {source} from "
            f"{TEMPEST_APP_NAME} ({TEMPEST_CONTAINER_NAME}) "
            f"to {destination} ..."
        )
        with console.status(progress_message):
            # Note: this is a workaround to run command to payload container
            # since python-libjuju does not support such feature. See related
            # bug: https://github.com/juju/python-libjuju/issues/1029
            if Path(destination).is_dir():
                # juju scp does not allow directory as destination
                destination = str(Path(destination, Path(source).name))
            subprocess.run(
                [
                    "juju",
                    "scp",
                    "--container",
                    TEMPEST_CONTAINER_NAME,
                    f"{unit}:{source}",
                    destination,
                ],
            )

    def _configure_preflight_check(self) -> False:
        """Preflight check for configure command."""
        enabled_plugins = PluginManager.enabled_plugins(self.deployment)
        if "observability" not in enabled_plugins:
            return False
        return True

    @click.command()
    def enable_plugin(self) -> None:
        """Enable OpenStack Integration Test Suite (tempest)."""
        super().enable_plugin()

    @click.command()
    def disable_plugin(self) -> None:
        """Disable OpenStack Integration Test Suite (tempest)."""
        super().disable_plugin()

    @click.command()
    @click.argument("options", nargs=-1)
    def configure_validation(self, options: Optional[List[str]] = None) -> None:
        """Configure validation plugin.

        Run without arguments to view available configuration options.

        Run with key=value args to set configuration values.
        For example: sunbeam configure validation schedule="*/30 * * * *"
        """
        if not self._configure_preflight_check():
            raise click.ClickException(
                "'observability' plugin is required for configuring validation plugin."
            )

        if not options:
            console.print(
                "Config options available: \n\n"
                "schedule: set a cron schedule for running periodic tests.  "
                "Empty disables.\n\n"
                "Run with key=value args to set configuration values.\n"
                'For example: sunbeam configure validation schedule="*/30 * * * *"'
            )
            return

        config_changes = validated_config_args(parse_config_args(options))

        if config_changes.schedule is not None:
            jhelper = JujuHelper(self.deployment.get_connected_controller())
            with console.status("Configuring validation plugin ..."):
                run_sync(
                    jhelper.set_application_config(
                        OPENSTACK_MODEL,
                        TEMPEST_APP_NAME,
                        config={"schedule": config_changes.schedule},
                    )
                )
                console.print(f"Schedule has been set to '{config_changes.schedule}'")

    @click.command()
    @click.option(
        "-r",
        "--regex",
        default="",
        help=(
            "A list of regexes, whitespace separated, used to select tests from"
            " the list."
        ),
    )
    @click.option(
        "-e",
        "--exclude-regex",
        default="",
        help="A single regex to exclude tests.",
    )
    @click.option(
        "-t",
        "--serial",
        is_flag=True,
        default=False,
        help="Run tests serially. By default, tests run in parallel.",
    )
    @click.option(
        "-l",
        "--test-list",
        default="",
        help=(
            "Use a predefined test list. See `sunbeam validation test-lists`"
            " for available test lists."
        ),
    )
    @click.option(
        "-o",
        "--output",
        type=click.Path(),
        default=None,
        help=(
            "Download the full log to output file. "
            "If not provided, the output can be retrieved later "
            "by running `sunbeam validation get-last-result`."
        ),
    )
    def run_validate_action(
        self,
        regex: str = "",
        exclude_regex: str = "",
        serial: bool = False,
        test_list: str = "",
        output: Optional[str] = None,
    ) -> None:
        """Run a set of tests on the sunbeam installation."""
        action_name = "validate"
        action_params = {
            "regex": regex,
            "exclude-regex": exclude_regex,
            "serial": serial,
            "test-list": test_list,
        }
        progress_message = "Running tempest to validate the sunbeam deployment ..."
        action_result = self._run_action_on_tempest_unit(
            action_name,
            action_params=action_params,
            progress_message=progress_message,
        )

        console.print(action_result.get("summary").strip())

        if output:
            self._copy_file_from_tempest_container(TEMPEST_VALIDATION_RESULT, output)

    @click.command()
    def run_get_lists_action(self) -> None:
        """Get supported test lists for validation."""
        action_name = "get-lists"
        progress_message = "Retrieving existing test lists from tempest charm ..."
        action_result = self._run_action_on_tempest_unit(
            action_name,
            action_params={},
            progress_message=progress_message,
        )
        console.print(action_result.get("stdout").strip())

    @click.command()
    @click.option(
        "-o",
        "--output",
        type=click.Path(),
        required=True,
        help="Download the last validation check result to output file.",
    )
    def run_get_last_result(self, output: str) -> None:
        """Get last validation result."""
        if not self._check_file_exist_in_tempest_container(TEMPEST_VALIDATION_RESULT):
            raise click.ClickException(
                (
                    f"Cannot find '{TEMPEST_VALIDATION_RESULT}'. "
                    "Have you run `sunbeam validation run` at least once?"
                )
            )
        self._copy_file_from_tempest_container(TEMPEST_VALIDATION_RESULT, output)

    @click.group()
    def validation_group(self):
        """Manage cloud validation functionality."""

    def commands(self) -> dict:
        """Dict of clickgroup along with commands."""
        commands = super().commands()
        try:
            enabled = self.enabled
        except ClusterServiceUnavailableException:
            LOG.debug(
                "Failed to query for plugin status, is cloud bootstrapped ?",
                exc_info=True,
            )
            enabled = False

        if enabled:
            commands.update(
                {
                    # sunbeam configure validation ...
                    "configure": [
                        {"name": "validation", "command": self.configure_validation}
                    ],
                    # add the validation subcommand group to the root group:
                    # sunbeam validation ...
                    "init": [{"name": "validation", "command": self.validation_group}],
                    # add the subcommands:
                    # sunbeam validation run ... etc.
                    "init.validation": [
                        {"name": "run", "command": self.run_validate_action},
                        {"name": "test-lists", "command": self.run_get_lists_action},
                        {
                            "name": "get-last-result",
                            "command": self.run_get_last_result,
                        },
                    ],
                }
            )
        return commands
