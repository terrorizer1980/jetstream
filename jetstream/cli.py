import logging
import os
import sys
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    TextIO,
    Tuple,
    Type,
    Union,
)

import attr
import click
import mozanalysis
import pytz
import toml

from . import bq_normalize_name, external_config
from .analysis import Analysis
from .argo import submit_workflow
from .bigquery_client import BigQueryClient
from .config import AnalysisSpec
from .dryrun import DryRunFailedError
from .errors import ExplicitSkipException, ValidationException
from .experimenter import ExperimentCollection
from .export_json import export_statistics_tables
from .external_config import ExternalConfigCollection
from .logging.bigquery_log_handler import BigQueryLogHandler
from .metadata import export_metadata
from .util import inclusive_date_range


def setup_logger(
    log_project_id, log_dataset_id, log_table_id, log_to_bigquery, client=None, capacity=50
):
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(asctime)s:%(name)s:%(message)s",
    )
    logger = logging.getLogger()

    if log_to_bigquery:
        bigquery_handler = BigQueryLogHandler(
            log_project_id, log_dataset_id, log_table_id, client, capacity
        )
        bigquery_handler.setLevel(logging.WARNING)
        logger.addHandler(bigquery_handler)


logger = logging.getLogger(__name__)


RECOGNIZED_EXPERIMENT_TYPES = ("pref", "addon", "message", "v6")


@attr.s
class AllType:
    """Sentinel value for AnalysisExecutor"""


All = AllType()


class ExecutorStrategy(Protocol):
    project_id: str

    def __init__(self, project_id: str, dataset_id: str, *args, **kwargs) -> None:
        ...

    def execute(
        self,
        worklist: Iterable[Tuple[str, datetime]],
        configuration_map: Optional[Mapping[str, TextIO]] = None,
    ) -> bool:
        ...


@attr.s(auto_attribs=True)
class ArgoExecutorStrategy:
    project_id: str
    dataset_id: str
    bucket: str
    zone: str
    cluster_id: str
    monitor_status: bool
    cluster_ip: Optional[str] = None
    cluster_cert: Optional[str] = None
    experiment_getter: Callable[[], ExperimentCollection] = ExperimentCollection.from_experimenter

    WORKLFOW_DIR = Path(__file__).parent / "workflows"
    RUN_WORKFLOW = WORKLFOW_DIR / "run.yaml"

    def execute(
        self,
        worklist: Iterable[Tuple[str, datetime]],
        configuration_map: Optional[Mapping[str, TextIO]] = None,
    ):
        if configuration_map is not None:
            raise Exception("Custom configurations are not supported when running with Argo")

        experiments_config: Dict[str, List[str]] = {}
        for (slug, date) in worklist:
            experiments_config.setdefault(slug, []).append(date.strftime("%Y-%m-%d"))

        experiments_config_list = [
            {"slug": slug, "dates": dates} for slug, dates in experiments_config.items()
        ]

        return submit_workflow(
            project_id=self.project_id,
            zone=self.zone,
            cluster_id=self.cluster_id,
            workflow_file=self.RUN_WORKFLOW,
            parameters={
                "experiments": experiments_config_list,
                "project_id": self.project_id,
                "dataset_id": self.dataset_id,
                "bucket": self.bucket,
            },
            monitor_status=self.monitor_status,
            cluster_ip=self.cluster_ip,
            cluster_cert=self.cluster_cert,
        )


@attr.s(auto_attribs=True)
class SerialExecutorStrategy:
    project_id: str
    dataset_id: str
    bucket: str
    analysis_class: Type = Analysis
    experiment_getter: Callable[[], ExperimentCollection] = ExperimentCollection.from_experimenter
    config_getter: Callable[
        [], ExternalConfigCollection
    ] = ExternalConfigCollection.from_github_repo

    def execute(
        self,
        worklist: Iterable[Tuple[str, datetime]],
        configuration_map: Optional[Mapping[str, TextIO]] = None,
    ):
        failed = False
        experiments = self.experiment_getter()
        for slug, date in worklist:
            try:
                experiment = experiments.with_slug(slug).experiments[0]
                spec = AnalysisSpec.default_for_experiment(experiment)
                if configuration_map and slug in configuration_map:
                    config_dict = toml.load(configuration_map[slug])
                    spec.merge(AnalysisSpec.from_dict(config_dict))
                else:
                    external_configs = self.config_getter()
                    if external_spec := external_configs.spec_for_experiment(slug):
                        spec.merge(external_spec)

                config = spec.resolve(experiment, external_configs)
                analysis = self.analysis_class(self.project_id, self.dataset_id, config)
                analysis.run(date)
                export_metadata(config, self.bucket, self.project_id)
            except ValidationException as e:
                # log custom Jetstream exceptions but let the workflow succeed;
                # this prevents Argo from retrying the analysis unnecessarily
                # when it is already clear that it won't succeed
                logger.exception(str(e), exc_info=e, extra={"experiment": slug})
            except Exception as e:
                failed = True
                logger.exception(str(e), exc_info=e, extra={"experiment": slug})
        return not failed


@attr.s(auto_attribs=True)
class AnalysisExecutor:
    project_id: str
    dataset_id: str
    bucket: str
    date: Union[datetime, AllType]
    experiment_slugs: Union[Iterable[str], AllType]
    configuration_map: Optional[Mapping[str, TextIO]] = attr.ib(None)
    recreate_enrollments: bool = False

    @staticmethod
    def _today() -> datetime:
        return datetime.combine(
            datetime.now(tz=pytz.utc).date() - timedelta(days=1),
            datetime.min.time(),
            tzinfo=pytz.utc,
        )

    def execute(
        self,
        strategy: ExecutorStrategy,
        *,
        experiment_getter: Callable[
            [], ExperimentCollection
        ] = ExperimentCollection.from_experimenter,
        config_getter: Callable[
            [], ExternalConfigCollection
        ] = ExternalConfigCollection.from_github_repo,
        today: Optional[datetime] = None,
    ) -> bool:
        run_experiments = self._experiments_to_analyse(experiment_getter)
        worklist = []

        for experiment in run_experiments:
            if self.date == All:
                today = today or self._today()
                end_date = min(
                    experiment.end_date + timedelta(days=1) or today,
                    today,
                )
                run_dates = inclusive_date_range(experiment.start_date, end_date)
            else:
                run_dates = [self.date]

            for run_date in run_dates:
                assert experiment.normandy_slug
                worklist.append((experiment.normandy_slug, run_date))

            if self.recreate_enrollments:
                self._delete_enrollment_table(experiment)

        return strategy.execute(worklist, self.configuration_map)

    def _delete_enrollment_table(self, experiment: mozanalysis.experiment.Experiment) -> None:
        """Deletes all enrollment table associated with the experiment."""
        print(f"Delete enrollment table for {experiment.normandy_slug}")
        client = BigQueryClient(project=self.project_id, dataset=self.dataset_id)
        normalized_slug = bq_normalize_name(experiment.normandy_slug)
        enrollments_table = f"{self.project_id}.{self.dataset_id}.enrollments_{normalized_slug}"
        client.delete_table(enrollments_table)

    def _experiments_to_analyse(
        self,
        experiment_getter: Callable[
            [], ExperimentCollection
        ] = ExperimentCollection.from_experimenter,
    ) -> List[mozanalysis.experiment.Experiment]:
        """Fetch experiments that are to be analysed."""
        experiments = experiment_getter()
        run_experiments = []

        if isinstance(self.experiment_slugs, AllType):
            if isinstance(self.date, AllType):
                raise ValueError("Declining to re-run all experiments for all time.")
            run_experiments = [
                e
                for e in (
                    experiments.end_on_or_after(self.date)
                    .of_type(RECOGNIZED_EXPERIMENT_TYPES)
                    .experiments
                )
                if e.normandy_slug is not None
            ]
        else:
            for slug in self.experiment_slugs:
                if e := experiments.with_slug(slug).experiments:
                    run_experiments.append(e[0])
                else:
                    logger.warning(
                        f"Slug {slug} provided but not found in Experimenter; skipping.",
                        extra={"experiment": slug},
                    )
        return run_experiments

    def ensure_enrollments(
        self,
        config_getter: Callable[
            [], ExternalConfigCollection
        ] = ExternalConfigCollection.from_github_repo,
        experiment_getter: Callable[
            [], ExperimentCollection
        ] = ExperimentCollection.from_experimenter,
    ) -> None:
        """Ensure that enrollment tables for experiment are up-to-date or re-create."""
        run_experiments = self._experiments_to_analyse(experiment_getter)
        for experiment in run_experiments:
            try:
                spec = AnalysisSpec.default_for_experiment(experiment)
                if self.configuration_map and experiment.normandy_slug in self.configuration_map:
                    config_dict = toml.load(self.configuration_map[experiment.normandy_slug])
                    spec.merge(AnalysisSpec.from_dict(config_dict))
                else:
                    external_configs = config_getter()
                    if external_spec := external_configs.spec_for_experiment(
                        experiment.normandy_slug
                    ):
                        spec.merge(external_spec)

                config = spec.resolve(experiment)
                analysis = Analysis(self.project_id, self.dataset_id, config)

                if self.date == All:
                    today = self._today()
                    end_date = min(
                        experiment.end_date or today,
                        today,
                    )
                else:
                    end_date = self.date

                analysis.ensure_enrollments(end_date)
            except Exception as e:
                logger.exception(str(e), exc_info=e, extra={"experiment": experiment.normandy_slug})
                raise e


@click.group()
@click.option(
    "--log_project_id",
    "--log-project-id",
    default="moz-fx-data-experiments",
    help="GCP project to write logs to",
)
@click.option(
    "--log_dataset_id",
    "--log-dataset-id",
    default="monitoring",
    help="Dataset to write logs to",
)
@click.option("--log_table_id", "--log-table-id", default="logs", help="Table to write logs to")
@click.option("--log_to_bigquery", "--log-to-bigquery", is_flag=True, default=False)
def cli(log_project_id, log_dataset_id, log_table_id, log_to_bigquery):
    setup_logger(log_project_id, log_dataset_id, log_table_id, log_to_bigquery)


class ClickDate(click.ParamType):
    name = "date"

    def convert(self, value, param, ctx):
        if isinstance(value, datetime):
            return value
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=pytz.utc)


project_id_option = click.option(
    "--project_id",
    "--project-id",
    default="moz-fx-data-experiments",
    help="Project to write to",
)
dataset_id_option = click.option(
    "--dataset_id", "--dataset-id", default="mozanalysis", help="Dataset to write to", required=True
)
zone_option = click.option(
    "--zone", default="us-central1-a", help="Kubernetes cluster zone", required=True
)
cluster_id_option = click.option(
    "--cluster_id",
    "--cluster-id",
    default="jetstream",
    help="Kubernetes cluster name",
    required=True,
)

experiment_slug_option = click.option(
    "--experiment_slug",
    "--experiment-slug",
    help="Experimenter or Normandy slug of the experiment to (re)run analysis for",
)

secret_config_file_option = click.option(
    "--i-solemnly-swear-i-am-up-to-no-good", "config_file", type=click.File("rt"), hidden=True
)

bucket_option = click.option(
    "--bucket", default="mozanalysis", help="GCS bucket to write to", required=True
)

argo_option = click.option(
    "--argo", is_flag=True, default=False, help="Run on Kubernetes with Argo"
)

return_status_option = click.option(
    "--return_status",
    "--return-status",
    is_flag=True,
    default=False,
    help="Return success/failed status code",
)

monitor_status_option = click.option(
    "--monitor_status",
    "--monitor-status",
    default=True,
    help="Monitor the status of the Argo workflow",
)

cluster_ip_option = click.option(
    "--cluster_ip",
    "--cluster-ip",
    help="Kubernetes cluster IP address",
)

cluster_cert_option = click.option(
    "--cluster_cert",
    "--cluster-cert",
    help="Kubernetes cluster certificate used for authenticating to the cluster",
)

recreate_enrollments_option = click.option(
    "--recreate_enrollments",
    "--recreate-enrollments",
    help="Recreate the enrollments tables",
    is_flag=True,
    default=False,
)


@cli.command()
@project_id_option
@dataset_id_option
@click.option(
    "--date",
    type=ClickDate(),
    help="Date for which experiments should be analyzed",
    metavar="YYYY-MM-DD",
    required=True,
)
@experiment_slug_option
@bucket_option
@secret_config_file_option
@recreate_enrollments_option
def run(
    project_id,
    dataset_id,
    date,
    experiment_slug,
    bucket,
    config_file,
    recreate_enrollments,
):
    """Runs analysis for the provided date."""
    analysis_executor = AnalysisExecutor(
        project_id=project_id,
        dataset_id=dataset_id,
        bucket=bucket,
        date=date,
        experiment_slugs=[experiment_slug] if experiment_slug else All,
        configuration_map={experiment_slug: config_file} if experiment_slug and config_file else {},
        recreate_enrollments=recreate_enrollments,
    )

    success = analysis_executor.execute(
        strategy=SerialExecutorStrategy(project_id, dataset_id, bucket)
    )

    sys.exit(0 if success else 1)


@cli.command()
@project_id_option
@dataset_id_option
@click.option(
    "--date",
    type=ClickDate(),
    help="Date for which experiments should be analyzed",
    metavar="YYYY-MM-DD",
    required=True,
)
@experiment_slug_option
@bucket_option
@zone_option
@cluster_id_option
@monitor_status_option
@cluster_ip_option
@cluster_cert_option
@recreate_enrollments_option
def run_argo(
    project_id,
    dataset_id,
    date,
    experiment_slug,
    bucket,
    zone,
    cluster_id,
    monitor_status,
    cluster_ip,
    cluster_cert,
    recreate_enrollments,
):
    """Runs analysis for the provided date using Argo."""
    strategy = ArgoExecutorStrategy(
        project_id=project_id,
        dataset_id=dataset_id,
        bucket=bucket,
        zone=zone,
        cluster_id=cluster_id,
        monitor_status=monitor_status,
        cluster_ip=cluster_ip,
        cluster_cert=cluster_cert,
    )

    AnalysisExecutor(
        project_id=project_id,
        dataset_id=dataset_id,
        bucket=bucket,
        date=date,
        experiment_slugs=[experiment_slug] if experiment_slug else All,
        recreate_enrollments=recreate_enrollments,
    ).execute(strategy=strategy)


@cli.command("rerun")
@experiment_slug_option
@project_id_option
@dataset_id_option
@bucket_option
@secret_config_file_option
@argo_option
@zone_option
@cluster_id_option
@monitor_status_option
@cluster_ip_option
@cluster_cert_option
@recreate_enrollments_option
def rerun(
    project_id,
    dataset_id,
    experiment_slug,
    bucket,
    config_file,
    argo,
    zone,
    cluster_id,
    monitor_status,
    cluster_ip,
    cluster_cert,
    recreate_enrollments,
):
    """Rerun all available analyses for a specific experiment."""
    strategy = SerialExecutorStrategy(project_id, dataset_id, bucket)

    if argo:
        strategy = ArgoExecutorStrategy(
            project_id=project_id,
            dataset_id=dataset_id,
            bucket=bucket,
            zone=zone,
            cluster_id=cluster_id,
            monitor_status=monitor_status,
            cluster_ip=cluster_ip,
            cluster_cert=cluster_cert,
        )

    AnalysisExecutor(
        project_id=project_id,
        dataset_id=dataset_id,
        bucket=bucket,
        date=All,
        experiment_slugs=[experiment_slug],
        configuration_map={experiment_slug: config_file} if config_file else None,
        recreate_enrollments=recreate_enrollments,
    ).execute(strategy=strategy)
    BigQueryClient(project_id, dataset_id).touch_tables(experiment_slug)


@cli.command()
@project_id_option
@dataset_id_option
@bucket_option
@experiment_slug_option
def export_statistics_to_json(project_id, dataset_id, bucket, experiment_slug):
    """Export all tables as JSON to a GCS bucket."""
    export_statistics_tables(project_id, dataset_id, bucket, experiment_slug)


@cli.command()
@project_id_option
@dataset_id_option
@bucket_option
@argo_option
@zone_option
@cluster_id_option
@monitor_status_option
@cluster_ip_option
@cluster_cert_option
@return_status_option
@recreate_enrollments_option
def rerun_config_changed(
    project_id,
    dataset_id,
    bucket,
    argo,
    zone,
    cluster_id,
    monitor_status,
    cluster_ip,
    cluster_cert,
    return_status,
    recreate_enrollments,
):
    """Rerun all available analyses for experiments with new or updated config files."""

    strategy = SerialExecutorStrategy(project_id, dataset_id, bucket)

    # get experiment-specific external configs
    external_configs = ExternalConfigCollection.from_github_repo()
    updated_external_configs = external_configs.updated_configs(project_id, dataset_id)

    if argo:
        strategy = ArgoExecutorStrategy(
            project_id=project_id,
            dataset_id=dataset_id,
            bucket=bucket,
            zone=zone,
            cluster_id=cluster_id,
            monitor_status=monitor_status,
            cluster_ip=cluster_ip,
            cluster_cert=cluster_cert,
        )

    success = AnalysisExecutor(
        project_id=project_id,
        dataset_id=dataset_id,
        bucket=bucket,
        date=All,
        experiment_slugs=[config.slug for config in updated_external_configs],
        recreate_enrollments=recreate_enrollments,
    ).execute(strategy=strategy)

    client = BigQueryClient(project_id, dataset_id)
    for config in updated_external_configs:
        client.touch_tables(config.slug)

    if return_status:
        sys.exit(0 if success else 1)


@cli.command("validate_config")
@click.argument("path", type=click.Path(exists=True), nargs=-1)
def validate_config(path: Iterable[os.PathLike]):
    """Validate config files."""
    dirty = False
    collection = ExperimentCollection.from_experimenter()

    for config_file in path:
        config_file = Path(config_file)
        if not config_file.is_file():
            continue
        if ".example" in config_file.suffixes:
            print(f"Skipping example config {config_file}")
            continue
        print(f"Evaluating {config_file}...")
        entity = external_config.entity_from_path(config_file)
        call = partial(entity.validate)
        if isinstance(entity, external_config.ExternalConfig):
            if (experiments := collection.with_slug(entity.slug).experiments) == []:
                print(f"No experiment with slug {entity.slug} in Experimenter.")
                dirty = True
                continue
            call = partial(entity.validate, experiment=experiments[0])
        try:
            call()
        except DryRunFailedError as e:
            print("Error evaluating SQL:")
            for i, line in enumerate(e.sql.split("\n")):
                print(f"{i+1: 4d} {line.rstrip()}")
            print("")
            print(str(e))
            dirty = True
        except ExplicitSkipException:
            print("Found an explicit skip directive; will ignore this experiment.")
    sys.exit(1 if dirty else 0)


@cli.command()
@project_id_option
@dataset_id_option
@bucket_option
@experiment_slug_option
@secret_config_file_option
@recreate_enrollments_option
def ensure_enrollments(
    project_id, dataset_id, bucket, experiment_slug, config_file, recreate_enrollments
):
    """Ensure that enrollment tables for experiment are up-to-date or re-create."""
    AnalysisExecutor(
        project_id=project_id,
        dataset_id=dataset_id,
        bucket=bucket,
        date=AnalysisExecutor._today(),
        experiment_slugs=[experiment_slug] if experiment_slug else All,
        configuration_map={experiment_slug: config_file} if config_file else None,
        recreate_enrollments=recreate_enrollments,
    ).ensure_enrollments()
