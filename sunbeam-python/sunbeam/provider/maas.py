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
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Tuple, Type

import click
import yaml
from rich.console import Console
from rich.table import Table
from snaphelpers import Snap

from sunbeam.clusterd.client import Client
from sunbeam.commands import resize as resize_cmds
from sunbeam.commands import utils
from sunbeam.commands.bootstrap_state import SetBootstrapped
from sunbeam.commands.clusterd import DeploySunbeamClusterdApplicationStep
from sunbeam.commands.deployment import Deployment, DeploymentsConfig, deployment_path
from sunbeam.commands.hypervisor import (
    AddHypervisorUnitStep,
    DeployHypervisorApplicationStep,
)
from sunbeam.commands.juju import (
    INFRASTRUCTURE_MODEL,
    AddCloudJujuStep,
    AddCredentialsJujuStep,
    AddInfrastructureModelStep,
)
from sunbeam.commands.maas import (
    AddMaasDeployment,
    DeploymentMachinesCheck,
    DeploymentNetworkingCheck,
    DeploymentTopologyCheck,
    MaasAddMachinesToClusterdStep,
    MaasBootstrapJujuStep,
    MaasClient,
    MaasConfigureMicrocephOSDStep,
    MaasDeployMachinesStep,
    MaasDeployment,
    MaasDeployMicrok8sApplicationStep,
    MaasSaveClusterdAddressStep,
    MaasSaveControllerStep,
    MaasScaleJujuStep,
    MachineNetworkCheck,
    MachineRequirementsCheck,
    MachineRolesCheck,
    MachineStorageCheck,
    NetworkMappingCompleteCheck,
    Networks,
    RoleTags,
    get_machine,
    get_network_mapping,
    is_maas_deployment,
    list_machines,
    list_machines_by_zone,
    list_spaces,
    map_space,
    str_presenter,
    unmap_space,
)
from sunbeam.commands.microceph import (
    AddMicrocephUnitsStep,
    DeployMicrocephApplicationStep,
)
from sunbeam.commands.microk8s import (
    AddMicrok8sCloudStep,
    AddMicrok8sUnitsStep,
    StoreMicrok8sConfigStep,
)
from sunbeam.commands.mysql import ConfigureMySQLStep
from sunbeam.commands.openstack import (
    DeployControlPlaneStep,
    PatchLoadBalancerServicesStep,
)
from sunbeam.commands.sunbeam_machine import (
    AddSunbeamMachineUnitsStep,
    DeploySunbeamMachineApplicationStep,
)
from sunbeam.commands.terraform import TerraformHelper, TerraformInitStep
from sunbeam.jobs.checks import (
    DiagnosticsCheck,
    DiagnosticsResult,
    JujuSnapCheck,
    LocalShareCheck,
    VerifyClusterdNotBootstrappedCheck,
)
from sunbeam.jobs.common import (
    CLICK_FAIL,
    CLICK_OK,
    CONTEXT_SETTINGS,
    FORMAT_TABLE,
    FORMAT_YAML,
    run_plan,
    run_preflight_checks,
)
from sunbeam.jobs.juju import JujuAccount, JujuHelper
from sunbeam.provider.base import ProviderBase
from sunbeam.utils import CatchGroup

LOG = logging.getLogger(__name__)
console = Console()

MAAS_TYPE = "maas"


@click.group("cluster", context_settings=CONTEXT_SETTINGS, cls=CatchGroup)
@click.pass_context
def cluster(ctx):
    """Manage the Sunbeam Cluster"""


@click.group("machine", context_settings=CONTEXT_SETTINGS, cls=CatchGroup)
@click.pass_context
def machine(ctx):
    """Manage machines."""
    pass


@click.group("zone", context_settings=CONTEXT_SETTINGS, cls=CatchGroup)
@click.pass_context
def zone(ctx):
    """Manage zones."""
    pass


@click.group("space", context_settings=CONTEXT_SETTINGS, cls=CatchGroup)
@click.pass_context
def space(ctx):
    """Manage spaces."""
    pass


@click.group("network", context_settings=CONTEXT_SETTINGS, cls=CatchGroup)
@click.pass_context
def network(ctx):
    """Manage networks."""
    pass


class MaasProvider(ProviderBase):
    def register_add_cli(self, add: click.Group) -> None:
        add.add_command(add_maas)

    def register_cli(
        self,
        init: click.Group,
        deployment: click.Group,
    ):
        init.add_command(cluster)
        cluster.add_command(bootstrap)
        cluster.add_command(deploy)
        cluster.add_command(list_nodes)
        cluster.add_command(resize_cmds.resize)
        deployment.add_command(machine)
        machine.add_command(list_machines_cmd)
        machine.add_command(show_machine_cmd)
        machine.add_command(validate_machine_cmd)
        deployment.add_command(zone)
        zone.add_command(list_zones_cmd)
        deployment.add_command(space)
        space.add_command(list_spaces_cmd)
        space.add_command(map_space_cmd)
        space.add_command(unmap_space_cmd)
        deployment.add_command(network)
        network.add_command(list_networks_cmd)
        deployment.add_command(validate_deployment_cmd)

    def deployment_type(self) -> Tuple[str, Type[Deployment]]:
        return MAAS_TYPE, MaasDeployment


@click.command()
@click.option("-a", "--accept-defaults", help="Accept all defaults.", is_flag=True)
@click.option(
    "-p",
    "--preseed",
    help="Preseed file.",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def bootstrap(
    preseed: Path | None = None,
    accept_defaults: bool = False,
) -> None:
    """Bootstrap the MAAS-backed deployment.

    Initialize the sunbeam cluster.
    """

    snap = Snap()

    preflight_checks = []
    preflight_checks.append(JujuSnapCheck())
    preflight_checks.append(LocalShareCheck())
    preflight_checks.append(VerifyClusterdNotBootstrappedCheck())
    run_preflight_checks(preflight_checks, console)

    deployment_location = deployment_path(snap)
    deployments = DeploymentsConfig.load(deployment_location)
    deployment = deployments.get_active()
    maas_client = MaasClient.from_deployment(deployment)

    if not is_maas_deployment(deployment):
        click.echo("Not a MAAS deployment.", sys.stderr)
        sys.exit(1)

    preflight_checks = []
    preflight_checks.append(NetworkMappingCompleteCheck(deployment))
    run_preflight_checks(preflight_checks, console)

    cloud_definition = JujuHelper.maas_cloud(deployment.name, deployment.url)
    credentials_definition = JujuHelper.maas_credential(
        cloud=deployment.name,
        credential=deployment.name,
        maas_apikey=deployment.token,
    )
    if deployment.juju_account is None:
        password = utils.random_string(32)
        deployment.juju_account = JujuAccount(user="admin", password=password)
        deployments.update_deployment(deployment)
        deployments.write()

    plan = []
    plan.append(AddCloudJujuStep(deployment.name, cloud_definition))
    plan.append(
        AddCredentialsJujuStep(
            cloud=deployment.name,
            credentials=deployment.name,
            definition=credentials_definition,
        )
    )
    plan.append(
        MaasBootstrapJujuStep(
            maas_client,
            deployment.name,
            cloud_definition["clouds"][deployment.name]["type"],
            deployment.controller,
            deployment.juju_account.password,
            accept_defaults=accept_defaults,
            preseed_file=preseed,
        )
    )
    plan.append(
        MaasScaleJujuStep(
            maas_client,
            deployment.controller,
        )
    )
    plan.append(
        MaasSaveControllerStep(deployment.controller, deployment.name, deployments)
    )
    run_plan(plan, console)

    if deployment.juju_account is None:
        console.print("Juju account should have been saved in previous step.")
        sys.exit(1)
    if deployment.juju_controller is None:
        console.print("Controller should have been saved in previous step.")
        sys.exit(1)
    jhelper = JujuHelper(None, Path())  # type: ignore
    jhelper.controller = deployment.get_connected_controller()
    plan2 = []
    plan2.append(DeploySunbeamClusterdApplicationStep(jhelper))
    plan2.append(MaasSaveClusterdAddressStep(jhelper, deployment.name, deployments))
    run_plan(plan2, console)

    client_url = deployment.clusterd_address
    if not client_url:
        console.print("Clusterd address should have been saved in previous step.")
        sys.exit(1)

    console.print("Bootstrap controller components complete.")


@click.command()
@click.option("-a", "--accept-defaults", help="Accept all defaults.", is_flag=True)
@click.option(
    "-p",
    "--preseed",
    help="Preseed file.",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def deploy(
    preseed: Path | None = None,
    accept_defaults: bool = False,
) -> None:
    """Deploy the MAAS-backed deployment.

    Deploy the sunbeam cluster.
    """
    snap = Snap()
    preflight_checks = []
    preflight_checks.append(JujuSnapCheck())
    preflight_checks.append(LocalShareCheck())
    preflight_checks.append(VerifyClusterdNotBootstrappedCheck())
    run_preflight_checks(preflight_checks, console)

    deployment_location = deployment_path(snap)
    deployments = DeploymentsConfig.load(deployment_location)
    deployment = deployments.get_active()
    maas_client = MaasClient.from_deployment(deployment)

    if not is_maas_deployment(deployment):
        console.print("Not a MAAS deployment.")
        sys.exit(1)

    if (
        deployment.clusterd_address is None
        or deployment.juju_account is None  # noqa: W503
        or deployment.juju_controller is None  # noqa: W503
    ):
        LOG.error(
            "Clusterd address: %r, Juju account: %r, Juju controller: %r",
            deployment.clusterd_address,
            deployment.juju_account,
            deployment.juju_controller,
        )
        console.print(
            f"{deployment.name!r} deployment is not complete, was bootstrap completed ?"
        )
        sys.exit(1)

    jhelper = JujuHelper(None, Path())  # type: ignore
    try:
        jhelper.controller = deployment.get_connected_controller()
    except OSError as e:
        console.print(f"Could not connect to controller: {e}")
        sys.exit(1)
    clusterd_plan = [MaasSaveClusterdAddressStep(jhelper, deployment.name, deployments)]
    run_plan(clusterd_plan, console)  # type: ignore

    client = Client.from_http(deployment.clusterd_address)
    preflight_checks = []
    preflight_checks.append(NetworkMappingCompleteCheck(deployment))
    run_preflight_checks(preflight_checks, console)
    tfplan_dirs = [
        "deploy-sunbeam-machine",
        "deploy-microk8s",
        "deploy-microceph",
        "deploy-openstack",
        "deploy-openstack-hypervisor",
    ]

    deployment_base_dir = snap.paths.user_common / "etc" / deployment.name
    for tfplan_dir in tfplan_dirs:
        src = snap.paths.snap / "etc" / tfplan_dir
        dst = deployment_base_dir / tfplan_dir
        LOG.debug(f"Updating {dst} from {src}...")
        shutil.copytree(src, dst, dirs_exist_ok=True)
    controller_env = dict(
        JUJU_USERNAME=deployment.juju_account.user,
        JUJU_PASSWORD=deployment.juju_account.password,
        JUJU_CONTROLLER_ADDRESSES=",".join(deployment.juju_controller.api_endpoints),
        JUJU_CA_CERT=deployment.juju_controller.ca_cert,
    )
    tfhelper_sunbeam_machine = TerraformHelper(
        path=deployment_base_dir / "deploy-sunbeam-machine",
        plan="sunbeam-machine-plan",
        backend="http",
        clusterd_address=deployment.clusterd_address,
        env=controller_env,
    )
    tfhelper_microk8s = TerraformHelper(
        path=deployment_base_dir / "deploy-microk8s",
        plan="microk8s-plan",
        backend="http",
        clusterd_address=deployment.clusterd_address,
        env=controller_env,
    )
    tfhelper_microceph = TerraformHelper(
        path=deployment_base_dir / "deploy-microceph",
        plan="microceph-plan",
        backend="http",
        clusterd_address=deployment.clusterd_address,
        env=controller_env,
    )
    tfhelper_openstack_deploy = TerraformHelper(
        path=deployment_base_dir / "deploy-openstack",
        plan="openstack-plan",
        backend="http",
        clusterd_address=deployment.clusterd_address,
        env=controller_env,
    )
    tfhelper_hypervisor_deploy = TerraformHelper(
        path=deployment_base_dir / "deploy-openstack-hypervisor",
        plan="hypervisor-plan",
        backend="http",
        clusterd_address=deployment.clusterd_address,
        env=controller_env,
    )

    plan = []
    plan.append(AddInfrastructureModelStep(jhelper))
    plan.append(MaasAddMachinesToClusterdStep(client, maas_client))
    plan.append(MaasDeployMachinesStep(client, jhelper))
    run_plan(plan, console)

    def _name_mapper(node: dict) -> str:
        return node["name"]

    control = list(
        map(_name_mapper, client.cluster.list_nodes_by_role(RoleTags.CONTROL.value))
    )
    nb_control = len(control)
    compute = list(
        map(_name_mapper, client.cluster.list_nodes_by_role(RoleTags.COMPUTE.value))
    )
    nb_compute = len(compute)
    storage = list(
        map(_name_mapper, client.cluster.list_nodes_by_role(RoleTags.STORAGE.value))
    )
    nb_storage = len(storage)
    workers = list(set(compute + control + storage))

    if nb_control + nb_compute + nb_storage < 3:
        console.print(
            "Deployments needs at least one of each role to work correctly:"
            f"\n\tcontrol: {len(control)}"
            f"\n\tcompute: {len(compute)}"
            f"\n\tstorage: {len(storage)}"
        )
        sys.exit(1)

    plan2 = []
    plan2.append(TerraformInitStep(tfhelper_sunbeam_machine))
    plan2.append(
        DeploySunbeamMachineApplicationStep(
            client,
            tfhelper_sunbeam_machine,
            jhelper,
            INFRASTRUCTURE_MODEL,
        )
    )
    plan2.append(
        AddSunbeamMachineUnitsStep(client, workers, jhelper, INFRASTRUCTURE_MODEL)
    )
    plan2.append(TerraformInitStep(tfhelper_microk8s))
    plan2.append(
        MaasDeployMicrok8sApplicationStep(
            client,
            maas_client,
            tfhelper_microk8s,
            jhelper,
            str(deployment.network_mapping[Networks.PUBLIC.value]),
            str(deployment.network_mapping[Networks.INTERNAL.value]),
            INFRASTRUCTURE_MODEL,
            preseed,
            accept_defaults,
        )
    )
    plan2.append(AddMicrok8sUnitsStep(client, control, jhelper, INFRASTRUCTURE_MODEL))
    plan2.append(StoreMicrok8sConfigStep(client, jhelper, INFRASTRUCTURE_MODEL))
    plan2.append(AddMicrok8sCloudStep(client, jhelper))
    plan2.append(TerraformInitStep(tfhelper_microceph))
    plan2.append(
        DeployMicrocephApplicationStep(
            client, tfhelper_microceph, jhelper, INFRASTRUCTURE_MODEL
        )
    )
    plan2.append(AddMicrocephUnitsStep(client, storage, jhelper, INFRASTRUCTURE_MODEL))
    plan2.append(
        MaasConfigureMicrocephOSDStep(
            client,
            maas_client,
            jhelper,
            storage,
            INFRASTRUCTURE_MODEL,
        )
    )
    plan2.append(TerraformInitStep(tfhelper_openstack_deploy))
    plan2.append(
        DeployControlPlaneStep(
            client,
            tfhelper_openstack_deploy,
            jhelper,
            "auto",
            "auto",  # TODO(gboutry): use the right values
            INFRASTRUCTURE_MODEL,
        )
    )
    plan2.append(ConfigureMySQLStep(jhelper))
    plan2.append(PatchLoadBalancerServicesStep(client))
    plan2.append(TerraformInitStep(tfhelper_hypervisor_deploy))
    plan2.append(
        DeployHypervisorApplicationStep(
            client,
            tfhelper_hypervisor_deploy,
            tfhelper_openstack_deploy,
            jhelper,
            INFRASTRUCTURE_MODEL,
        )
    )
    plan2.append(AddHypervisorUnitStep(client, compute, jhelper, INFRASTRUCTURE_MODEL))
    plan2.append(SetBootstrapped(client))
    run_plan(plan2, console)

    console.print(
        f"Deployment complete with {nb_control} control,"
        f" {nb_compute} compute and {nb_storage} storage nodes."
        f" Total nodes in cluster: {len(workers)}"
    )


@click.command("list")
@click.option(
    "-f",
    "--format",
    type=click.Choice([FORMAT_TABLE, FORMAT_YAML]),
    default=FORMAT_TABLE,
    help="Output format.",
)
def list_nodes(format: str) -> None:
    """List nodes in the custer."""
    raise NotImplementedError


@click.command("maas")
@click.option("-n", "--name", type=str, prompt=True, help="Name of the deployment")
@click.option("-t", "--token", type=str, prompt=True, help="API token")
@click.option("-u", "--url", type=str, prompt=True, help="API URL")
@click.option("-r", "--resource-pool", type=str, prompt=True, help="Resource pool")
def add_maas(name: str, token: str, url: str, resource_pool: str) -> None:
    """Add MAAS-backed deployment to registered deployments."""
    preflight_checks = [
        LocalShareCheck(),
        VerifyClusterdNotBootstrappedCheck(),
    ]
    run_preflight_checks(preflight_checks, console)

    snap = Snap()
    path = deployment_path(snap)
    deployments = DeploymentsConfig.load(path)
    plan = []
    plan.append(
        AddMaasDeployment(
            deployments,
            MaasDeployment(
                name=name, token=token, url=url, resource_pool=resource_pool
            ),
        )
    )
    run_plan(plan, console)
    click.echo(f"MAAS deployment {name} added.")


@click.command("list")
@click.option(
    "--format",
    type=click.Choice([FORMAT_TABLE, FORMAT_YAML]),
    default=FORMAT_TABLE,
    help="Output format",
)
def list_machines_cmd(format: str) -> None:
    """List machines in active deployment."""
    preflight_checks = [
        LocalShareCheck(),
    ]
    run_preflight_checks(preflight_checks, console)

    snap = Snap()

    deployment_location = deployment_path(snap)
    deployments_config = DeploymentsConfig.load(deployment_location)

    client = MaasClient.from_deployment(deployments_config.get_active())
    machines = list_machines(client)
    if format == FORMAT_TABLE:
        table = Table()
        table.add_column("Machine")
        table.add_column("Roles")
        table.add_column("Zone")
        table.add_column("Status")
        for machine in machines:
            hostname = machine["hostname"]
            status = machine["status"]
            zone = machine["zone"]
            roles = ", ".join(machine["roles"])
            table.add_row(hostname, roles, zone, status)
        console.print(table)
    elif format == FORMAT_YAML:
        console.print(yaml.dump(machines), end="")


@click.command("show")
@click.argument("hostname", type=str)
@click.option(
    "--format",
    type=click.Choice([FORMAT_TABLE, FORMAT_YAML]),
    default=FORMAT_TABLE,
    help="Output format",
)
def show_machine_cmd(hostname: str, format: str) -> None:
    """Show machine in active deployment."""
    preflight_checks = [
        LocalShareCheck(),
    ]
    run_preflight_checks(preflight_checks, console)

    snap = Snap()
    deployment_location = deployment_path(snap)
    deployments_config = DeploymentsConfig.load(deployment_location)

    client = MaasClient.from_deployment(deployments_config.get_active())
    machine = get_machine(client, hostname)
    header = "[bold]{}[/bold]"
    if format == FORMAT_TABLE:
        table = Table(show_header=False)
        table.add_row(header.format("Name"), machine["hostname"])
        table.add_row(header.format("Roles"), ", ".join(machine["roles"]))
        table.add_row(header.format("Network Spaces"), ", ".join(machine["spaces"]))
        table.add_row(
            header.format(
                "Storage Devices",
            ),
            ", ".join(
                f"{tag}({len(devices)})" for tag, devices in machine["storage"].items()
            ),
        )
        table.add_row(header.format("Zone"), machine["zone"])
        table.add_row(header.format("Status"), machine["status"])
        console.print(table)
    elif format == FORMAT_YAML:
        console.print(yaml.dump(machine), end="")


def _zones_table(zone_machines: dict[str, list[dict]]) -> Table:
    table = Table()
    table.add_column("Zone")
    table.add_column("Machines")
    for zone, machines in zone_machines.items():
        table.add_row(zone, str(len(machines)))
    return table


def _zones_roles_table(zone_machines: dict[str, list[dict]]) -> Table:
    table = Table(padding=(0, 0), show_header=False)

    zone_table = Table(
        title="\u00A0",  # non-breaking space to have zone same height as roles
        show_edge=False,
        show_header=False,
        expand=True,
    )
    zone_table.add_column("#not_shown#", justify="center")
    zone_table.add_row("[bold]Zone[/bold]", end_section=True)

    machine_table = Table(
        show_edge=False,
        show_header=True,
        title="Machines",
        title_style="bold",
        expand=True,
    )
    for role in RoleTags.values():
        machine_table.add_column(role, justify="center")
    machine_table.add_column("total", justify="center")
    for zone, machines in zone_machines.items():
        zone_table.add_row(zone)
        role_count = Counter()
        for machine in machines:
            role_count.update(machine["roles"])
        role_nb = [str(role_count.get(role, 0)) for role in RoleTags.values()]
        role_nb += [str(len(machines))]  # total
        machine_table.add_row(*role_nb)

    table.add_row(zone_table, machine_table)
    return table


@click.command("list")
@click.option(
    "--roles",
    is_flag=True,
    show_default=True,
    default=False,
    help="List roles",
)
@click.option(
    "--format",
    type=click.Choice([FORMAT_TABLE, FORMAT_YAML]),
    default=FORMAT_TABLE,
    help="Output format",
)
def list_zones_cmd(roles: bool, format: str) -> None:
    """List zones in active deployment."""
    preflight_checks = [
        LocalShareCheck(),
    ]
    run_preflight_checks(preflight_checks, console)

    snap = Snap()
    deployment_location = deployment_path(snap)
    deployments_config = DeploymentsConfig.load(deployment_location)

    client = MaasClient.from_deployment(deployments_config.get_active())

    zones_machines = list_machines_by_zone(client)
    if format == FORMAT_TABLE:
        if roles:
            table = _zones_roles_table(zones_machines)
        else:
            table = _zones_table(zones_machines)
        console.print(table)
    elif format == FORMAT_YAML:
        console.print(yaml.dump(zones_machines), end="")


@click.command("list")
@click.option(
    "--format",
    type=click.Choice([FORMAT_TABLE, FORMAT_YAML]),
    default=FORMAT_TABLE,
    help="Output format",
)
def list_spaces_cmd(format: str) -> None:
    """List spaces in MAAS deployment."""
    preflight_checks = [
        LocalShareCheck(),
    ]
    run_preflight_checks(preflight_checks, console)

    snap = Snap()
    deployment_location = deployment_path(snap)
    deployments_config = DeploymentsConfig.load(deployment_location)

    client = MaasClient.from_deployment(deployments_config.get_active())
    spaces = list_spaces(client)
    if format == FORMAT_TABLE:
        table = Table()
        table.add_column("Space")
        table.add_column("Subnets", max_width=80)
        for space in spaces:
            table.add_row(space["name"], ", ".join(space["subnets"]))
        console.print(table)
    elif format == FORMAT_YAML:
        console.print(yaml.dump(spaces), end="")


@click.command("map")
@click.argument("space")
@click.argument("network", type=click.Choice(Networks.values()))
def map_space_cmd(space: str, network: str) -> None:
    """Map space to network."""
    preflight_checks = [
        LocalShareCheck(),
    ]
    run_preflight_checks(preflight_checks, console)

    snap = Snap()
    deployment_location = deployment_path(snap)
    deployments_config = DeploymentsConfig.load(deployment_location)

    client = MaasClient.from_deployment(deployments_config.get_active())
    map_space(deployments_config, client, space, Networks(network))
    console.print(f"Space {space} mapped to network {network}.")


@click.command("unmap")
@click.argument("network", type=click.Choice(Networks.values()))
def unmap_space_cmd(network: str) -> None:
    """Unmap space from network."""
    preflight_checks = [
        LocalShareCheck(),
    ]
    run_preflight_checks(preflight_checks, console)

    snap = Snap()
    deployment_location = deployment_path(snap)
    deployments_config = DeploymentsConfig.load(deployment_location)

    unmap_space(deployments_config, Networks(network))
    console.print(f"Space unmapped from network {network}.")


@click.command("list")
@click.option(
    "--format",
    type=click.Choice([FORMAT_TABLE, FORMAT_YAML]),
    default=FORMAT_TABLE,
    help="Output format",
)
def list_networks_cmd(format: str):
    """List networks and associated spaces."""
    preflight_checks = [
        LocalShareCheck(),
    ]
    run_preflight_checks(preflight_checks, console)

    snap = Snap()
    deployment_location = deployment_path(snap)
    deployments_config = DeploymentsConfig.load(deployment_location)

    mapping = get_network_mapping(deployments_config)
    if format == FORMAT_TABLE:
        table = Table()
        table.add_column("Network")
        table.add_column("MAAS Space")
        for network, space in mapping.items():
            table.add_row(network, space or "[italic]<unmapped>[italic]")
        console.print(table)
    elif format == FORMAT_YAML:
        console.print(yaml.dump(mapping), end="")


def _run_maas_checks(checks: list[DiagnosticsCheck], console: Console) -> list[dict]:
    """Run checks sequentially.

    Runs each checks, logs whether the check passed or failed.
    Prints to console every result.
    """
    check_results = []
    for check in checks:
        LOG.debug(f"Starting check {check.name!r}")
        message = f"{check.description}..."
        with console.status(message):
            results = check.run()
            if not results:
                raise ValueError(f"{check.name!r} returned no results.")

            if isinstance(results, DiagnosticsResult):
                results = [results]

            for result in results:
                LOG.debug(f"{result.name=!r}, {result.passed=!r}, {result.message=!r}")
                console.print(
                    message,
                    result.message,
                    "-",
                    CLICK_OK if result.passed else CLICK_FAIL,
                )
                check_results.append(result.to_dict())
    return check_results


def _run_maas_meta_checks(
    checks: list[DiagnosticsCheck], console: Console
) -> list[dict]:
    """Run checks sequentially.

    Runs each checks, logs whether the check passed or failed.
    Only prints to console last check result.
    """
    check_results = []

    for check in checks:
        LOG.debug(f"Starting check {check.name!r}")
        message = f"{check.description}..."
        with console.status(message):
            results = check.run()
            if not results:
                raise ValueError(f"{check.name!r} returned no results.")
            if isinstance(results, DiagnosticsResult):
                results = [results]
            for result in results:
                check_results.append(result.to_dict())
            console.print(message, CLICK_OK if results[-1].passed else CLICK_FAIL)
    return check_results


def _save_report(snap: Snap, name: str, report: list[dict]) -> str:
    """Save report to filesystem."""
    reports = snap.paths.user_common / "reports"
    if not reports.exists():
        reports.mkdir(parents=True)
    report_path = reports / f"{name}-{datetime.now():%Y%m%d-%H%M%S.%f}.yaml"
    with report_path.open("w") as fd:
        yaml.add_representer(str, str_presenter)
        yaml.dump(report, fd)
    return str(report_path.absolute())


@click.command("validate")
@click.argument("machine", type=str)
def validate_machine_cmd(machine: str):
    """Validate machine configuration."""
    preflight_checks = [
        LocalShareCheck(),
    ]
    run_preflight_checks(preflight_checks, console)

    snap = Snap()
    deployment_location = deployment_path(snap)
    deployments_config = DeploymentsConfig.load(deployment_location)
    deployment = deployments_config.get_active()
    if not is_maas_deployment(deployment):
        raise ValueError("Not a MAAS deployment.")

    client = MaasClient.from_deployment(deployment)
    with console.status(f"Fetching {machine} ..."):
        try:
            machine_obj = get_machine(client, machine)
            LOG.debug(f"{machine_obj=!r}")
        except ValueError as e:
            console.print("Error:", e)
            sys.exit(1)
    validation_checks = [
        MachineRolesCheck(machine_obj),
        MachineNetworkCheck(deployment, machine_obj),
        MachineStorageCheck(machine_obj),
        MachineRequirementsCheck(machine_obj),
    ]
    report = _run_maas_checks(validation_checks, console)
    report_path = _save_report(snap, "validate-machine-" + machine, report)
    console.print(f"Report saved to {report_path!r}")


@click.command("validate")
def validate_deployment_cmd():
    """Validate deployment."""
    preflight_checks = [
        LocalShareCheck(),
    ]
    run_preflight_checks(preflight_checks, console)
    snap = Snap()
    path = deployment_path(snap)
    deployments_config = DeploymentsConfig.load(path)
    deployment = deployments_config.get_active()
    if not is_maas_deployment(deployment):
        raise ValueError("Not a MAAS deployment.")
    client = MaasClient.from_deployment(deployment)
    with console.status(f"Fetching {deployment.name} machines ..."):
        try:
            machines = list_machines(client)
        except ValueError as e:
            console.print("Error:", e)
            sys.exit(1)
    validation_checks = [
        DeploymentMachinesCheck(deployment, machines),
        DeploymentTopologyCheck(machines),
        DeploymentNetworkingCheck(client, deployment),
    ]
    report = _run_maas_meta_checks(validation_checks, console)
    report_path = _save_report(snap, "validate-deployment-" + deployment.name, report)
    console.print(f"Report saved to {report_path!r}")
