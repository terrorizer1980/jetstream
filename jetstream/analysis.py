import google
import logging
import os
import re
from datetime import datetime, timedelta
from textwrap import dedent
from typing import Any, Dict, List, Optional

import attr
import dask
import mozanalysis
from dask.distributed import Client, LocalCluster
from google.cloud.exceptions import Conflict
from google.cloud import bigquery
from mozanalysis.experiment import TimeLimits
from mozanalysis.utils import add_days
from pandas import DataFrame

import jetstream.errors as errors
from jetstream.bigquery_client import BigQueryClient
from jetstream.config import AnalysisConfiguration
from jetstream.dryrun import dry_run_query
from jetstream.statistics import Count, StatisticResult, StatisticResultCollection, Summary

from . import AnalysisPeriod, bq_normalize_name

logger = logging.getLogger(__name__)

DASK_DASHBOARD_ADDRESS = "127.0.0.1:8782"
DASK_N_PROCESSES = int(os.getenv("JETSTREAM_PROCESSES", 0)) or None  # Defaults to number of CPUs

_dask_cluster = None


@attr.s(auto_attribs=True)
class Analysis:
    """Wrapper for analysing experiments."""

    project: str
    dataset: str
    config: AnalysisConfiguration

    @property
    def bigquery(self):
        return BigQueryClient(project=self.project, dataset=self.dataset)

    def _get_timelimits_if_ready(
        self, period: AnalysisPeriod, current_date: datetime
    ) -> Optional[TimeLimits]:
        """
        Returns a TimeLimits instance if experiment is due for analysis.
        Otherwise returns None.
        """
        prior_date = current_date - timedelta(days=1)
        prior_date_str = prior_date.strftime("%Y-%m-%d")
        current_date_str = current_date.strftime("%Y-%m-%d")

        dates_enrollment = self.config.experiment.proposed_enrollment + 1

        if self.config.experiment.start_date is None:
            return None

        time_limits_args = {
            "first_enrollment_date": self.config.experiment.start_date.strftime("%Y-%m-%d"),
            "num_dates_enrollment": dates_enrollment,
        }

        if period != AnalysisPeriod.OVERALL:
            try:
                current_time_limits = TimeLimits.for_ts(
                    last_date_full_data=current_date_str,
                    time_series_period=period.adjective,
                    **time_limits_args,
                )
            except ValueError:
                # There are no analysis windows yet.
                # TODO: Add a more specific check.
                return None

            try:
                prior_time_limits = TimeLimits.for_ts(
                    last_date_full_data=prior_date_str,
                    time_series_period=period.adjective,
                    **time_limits_args,
                )
            except ValueError:
                # We have an analysis window today, and we didn't yesterday,
                # so we must have just closed our first window.
                return current_time_limits

            if len(current_time_limits.analysis_windows) == len(prior_time_limits.analysis_windows):
                # No new data today
                return None

            return current_time_limits

        assert period == AnalysisPeriod.OVERALL
        if (
            self.config.experiment.end_date is None
            or self.config.experiment.end_date.date() != current_date.date()
            or self.config.experiment.status != "Complete"
        ):
            return None

        if self.config.experiment.end_date is None:
            return None

        analysis_length_dates = (
            (self.config.experiment.end_date - self.config.experiment.start_date).days
            - dates_enrollment
            + 1
        )

        if analysis_length_dates < 0:
            raise errors.EnrollmentLongerThanAnalysisException(self.config.experiment.normandy_slug)

        return TimeLimits.for_single_analysis_window(
            last_date_full_data=prior_date_str,
            analysis_start_days=0,
            analysis_length_dates=analysis_length_dates,
            **time_limits_args,
        )

    def _table_name(self, window_period: str, window_index: int) -> str:
        assert self.config.experiment.normandy_slug is not None
        normalized_slug = bq_normalize_name(self.config.experiment.normandy_slug)
        return "_".join([normalized_slug, window_period, str(window_index)])

    def _publish_view(self, window_period: AnalysisPeriod, table_prefix=None):
        assert self.config.experiment.normandy_slug is not None
        normalized_slug = bq_normalize_name(self.config.experiment.normandy_slug)
        view_name = "_".join([normalized_slug, window_period.adjective])
        wildcard_expr = "_".join([normalized_slug, window_period.value, "*"])

        if table_prefix:
            normalized_prefix = bq_normalize_name(table_prefix)
            view_name = "_".join([normalized_prefix, view_name])
            wildcard_expr = "_".join([normalized_prefix, wildcard_expr])

        sql = dedent(
            f"""
            CREATE OR REPLACE VIEW `{self.project}.{self.dataset}.{view_name}` AS (
                SELECT
                    *,
                    CAST(_TABLE_SUFFIX AS int64) AS window_index
                FROM `{self.project}.{self.dataset}.{wildcard_expr}`
            )
            """
        )
        self.bigquery.execute(sql)

    @dask.delayed
    def calculate_metrics(
        self,
        exp: mozanalysis.experiment.Experiment,
        time_limits: TimeLimits,
        period: AnalysisPeriod,
        dry_run: bool,
    ):
        """
        Calculate metrics for a specific experiment.
        Returns the BigQuery table results are written to.
        """

        window = len(time_limits.analysis_windows)
        last_analysis_window = time_limits.analysis_windows[-1]
        # TODO: Add this functionality to TimeLimits.
        last_window_limits = attr.evolve(
            time_limits,
            analysis_windows=[last_analysis_window],
            first_date_data_required=add_days(
                time_limits.first_enrollment_date, last_analysis_window.start
            ),
        )

        res_table_name = self._table_name(period.value, window)
        normalized_slug = bq_normalize_name(self.config.experiment.normandy_slug)
        enrollments_table = f"enrollments_{normalized_slug}"

        if dry_run:
            logger.info(
                "Dry run; not actually calculating %s metrics for %s",
                period.value,
                self.config.experiment.normandy_slug,
            )
        else:
            logger.info(
                "Executing query for %s (%s)",
                self.config.experiment.normandy_slug,
                period.value,
            )

            metrics_sql = exp.build_metrics_query(
                {m.metric for m in self.config.metrics[period]},
                last_window_limits,
                enrollments_table,
            )

            self.bigquery.execute(metrics_sql, res_table_name)
            self._publish_view(period)

        return res_table_name

    @dask.delayed
    def calculate_statistics(
        self,
        metric: Summary,
        segment_data: DataFrame,
        segment: str,
    ) -> StatisticResultCollection:
        """
        Run statistics on metric.
        """
        return metric.run(segment_data, self.config.experiment).set_segment(segment)

    @dask.delayed
    def counts(self, segment_data: DataFrame, segment: str) -> StatisticResultCollection:
        """Count and missing count statistics."""
        counts = (
            Count()
            .transform(segment_data, "*", "*", self.config.experiment.normandy_slug)
            .set_segment(segment)
        ).to_dict()["data"]

        return StatisticResultCollection(
            counts
            + [
                StatisticResult(
                    metric="identity",
                    statistic="count",
                    parameter=None,
                    branch=b.slug,
                    comparison=None,
                    comparison_to_branch=None,
                    ci_width=None,
                    point=0,
                    lower=None,
                    upper=None,
                    segment=segment,
                )
                for b in self.config.experiment.branches
                if b.slug not in {c["branch"] for c in counts}
            ]
        )

    @dask.delayed
    def subset_to_segment(self, segment: str, metrics_data: DataFrame) -> DataFrame:
        """Return metrics data for segment"""
        if segment != "all":
            if segment not in metrics_data.columns:
                raise ValueError(f"Segment {segment} not in metrics table")
            segment_data = metrics_data[metrics_data[segment]]
        else:
            segment_data = metrics_data

        return segment_data

    def check_runnable(self, current_date: Optional[datetime] = None) -> bool:
        if self.config.experiment.normandy_slug is None:
            # some experiments do not have a normandy slug
            raise errors.NoSlugException()

        if self.config.experiment.skip:
            raise errors.ExplicitSkipException(self.config.experiment.normandy_slug)

        if self.config.experiment.is_high_population:
            raise errors.HighPopulationException(self.config.experiment.normandy_slug)

        if not self.config.experiment.proposed_enrollment:
            raise errors.NoEnrollmentPeriodException(self.config.experiment.normandy_slug)

        if self.config.experiment.start_date is None:
            raise errors.NoStartDateException(self.config.experiment.normandy_slug)

        if (
            current_date
            and self.config.experiment.end_date
            and self.config.experiment.end_date < current_date
        ):
            raise errors.EndedException(self.config.experiment.normandy_slug)

        return True

    def _app_id_to_bigquery_dataset(self, app_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]", "_", app_id)

    def validate(self) -> None:
        self.check_runnable()
        assert self.config.experiment.start_date is not None  # for mypy

        dates_enrollment = self.config.experiment.proposed_enrollment + 1

        if self.config.experiment.end_date is not None:
            end_date = self.config.experiment.end_date
            analysis_length_dates = (
                (end_date - self.config.experiment.start_date).days - dates_enrollment + 1
            )
        else:
            analysis_length_dates = 21  # arbitrary
            end_date = self.config.experiment.start_date + timedelta(
                days=analysis_length_dates + dates_enrollment - 1
            )

        if analysis_length_dates < 0:
            logging.error(
                "Proposed enrollment longer than analysis dates length:"
                + f"{self.config.experiment.normandy_slug}"
            )
            raise Exception("Cannot validate experiment")

        limits = TimeLimits.for_single_analysis_window(
            last_date_full_data=end_date.strftime("%Y-%m-%d"),
            analysis_start_days=0,
            analysis_length_dates=analysis_length_dates,
            first_enrollment_date=self.config.experiment.start_date.strftime("%Y-%m-%d"),
            num_dates_enrollment=dates_enrollment,
        )

        exp = mozanalysis.experiment.Experiment(
            experiment_slug=self.config.experiment.normandy_slug,
            start_date=self.config.experiment.start_date.strftime("%Y-%m-%d"),
            app_id=self._app_id_to_bigquery_dataset(self.config.experiment.app_id),
        )

        metrics = set()
        for v in self.config.metrics.values():
            metrics |= {m.metric for m in v}

        enrollments_sql = exp.build_enrollments_query(
            limits,
            self.config.experiment.platform.enrollments_query_type,
            self.config.experiment.enrollment_query,
            self.config.experiment.segments,
        )

        dry_run_query(enrollments_sql)

        metrics_sql = exp.build_metrics_query(
            metrics,
            limits,
            "enrollments_table",
        )

        # enrollments_table doesn't get created when performing a dry run;
        # the metrics SQL is modified to include a subquery for a mock enrollments_table
        # A UNION ALL is required here otherwise the dry run fails with
        # "cannot query over table without filter over columns"
        metrics_sql = metrics_sql.replace(
            "WITH analysis_windows AS (",
            """WITH enrollments_table AS (
                SELECT '00000' AS client_id,
                    'test' AS branch,
                    DATE('2020-01-01') AS enrollment_date
                UNION ALL
                SELECT '00000' AS client_id,
                    'test' AS branch,
                    DATE('2020-01-01') AS enrollment_date
            ), analysis_windows AS (""",
        )

        dry_run_query(metrics_sql)

    @dask.delayed
    def save_statistics(
        self,
        period: AnalysisPeriod,
        segment_results: List[Dict[str, Any]],
        metrics_table: str,
    ):
        """Write statistics to BigQuery."""
        job_config = bigquery.LoadJobConfig()
        job_config.schema = StatisticResult.bq_schema
        job_config.write_disposition = bigquery.job.WriteDisposition.WRITE_TRUNCATE

        # wait for the job to complete
        self.bigquery.load_table_from_json(
            segment_results, f"statistics_{metrics_table}", job_config=job_config
        )

        self._publish_view(period, table_prefix="statistics")

    def run(self, current_date: datetime, dry_run: bool = False) -> None:
        """
        Run analysis using mozanalysis for a specific experiment.
        """
        global _dask_cluster
        logger.info("Analysis.run invoked for experiment %s", self.config.experiment.normandy_slug)

        self.check_runnable(current_date)
        assert self.config.experiment.start_date is not None  # for mypy

        self.ensure_enrollments(current_date)

        # set up dask
        _dask_cluster = _dask_cluster or LocalCluster(
            dashboard_address=DASK_DASHBOARD_ADDRESS,
            processes=True,
            threads_per_worker=1,
            n_workers=DASK_N_PROCESSES,
        )
        client = Client(_dask_cluster)

        # prepare dask tasks
        results = []
        table_to_dataframe = dask.delayed(self.bigquery.table_to_dataframe)

        for period in self.config.metrics:
            time_limits = self._get_timelimits_if_ready(period, current_date)

            if time_limits is None:
                logger.info(
                    "Skipping %s (%s); not ready",
                    self.config.experiment.normandy_slug,
                    period.value,
                )
                continue

            exp = mozanalysis.experiment.Experiment(
                experiment_slug=self.config.experiment.normandy_slug,
                start_date=self.config.experiment.start_date.strftime("%Y-%m-%d"),
                app_id=self._app_id_to_bigquery_dataset(self.config.experiment.app_id),
            )

            metrics_table = self.calculate_metrics(exp, time_limits, period, dry_run)

            if dry_run:
                logger.info(
                    "Not calculating statistics %s (%s); dry run",
                    self.config.experiment.normandy_slug,
                    period.value,
                )
                results.append(metrics_table)
                continue

            metrics_data = table_to_dataframe(metrics_table)

            segment_results = []

            segment_labels = ["all"] + [s.name for s in self.config.experiment.segments]
            for segment in segment_labels:
                segment_data = self.subset_to_segment(segment, metrics_data)
                for m in self.config.metrics[period]:
                    segment_results += self.calculate_statistics(
                        m,
                        segment_data,
                        segment,
                    ).to_dict()["data"]

                segment_results += self.counts(segment_data, segment).to_dict()["data"]

            results.append(self.save_statistics(period, segment_results, metrics_table))

        result_futures = client.compute(results)
        client.gather(result_futures)  # block until futures have finished

    def ensure_enrollments(self, current_date: datetime) -> None:
        """Ensure that enrollment tables for experiment are up-to-date or re-create."""
        time_limits = self._get_timelimits_if_ready(AnalysisPeriod.DAY, current_date)

        if time_limits is None:
            logger.info("Skipping %s (%s); not ready", self.config.experiment.normandy_slug)
            return

        if self.config.experiment.start_date is None:
            raise errors.NoStartDateException(self.config.experiment.normandy_slug)

        normalized_slug = bq_normalize_name(self.config.experiment.normandy_slug)
        enrollments_table = f"enrollments_{normalized_slug}"

        logger.info(f"Create {enrollments_table}")
        exp = mozanalysis.experiment.Experiment(
            experiment_slug=self.config.experiment.normandy_slug,
            start_date=self.config.experiment.start_date.strftime("%Y-%m-%d"),
            app_id=self._app_id_to_bigquery_dataset(self.config.experiment.app_id),
        )
        enrollments_sql = exp.build_enrollments_query(
            time_limits,
            self.config.experiment.platform.enrollments_query_type,
            self.config.experiment.enrollment_query,
            self.config.experiment.segments,
        )

        try:
            self.bigquery.execute(
                enrollments_sql,
                enrollments_table,
                google.cloud.bigquery.job.WriteDisposition.WRITE_EMPTY,
            )
        except Conflict:
            pass
