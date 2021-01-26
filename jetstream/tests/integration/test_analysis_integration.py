import datetime
import datetime as dt
from pathlib import Path

import dask
import mozanalysis
import pytz
from mozanalysis.metrics import DataSource, Metric, agg_sum
from mozanalysis.segments import Segment, SegmentDataSource

from jetstream import AnalysisPeriod
from jetstream.analysis import Analysis
from jetstream.config import AnalysisSpec, Summary
from jetstream.experimenter import Branch, Experiment
from jetstream.statistics import BootstrapMean

TEST_DIR = Path(__file__).parent.parent


class TestAnalysisIntegration:
    def mock_local_cluster(self, monkeypatch):
        orig_cluster = dask.distributed.LocalCluster.__init__
        # if processes are used then `build_query_test_project` gets ignored

        def mock_cluster(
            instance, dashboard_address, processes, threads_per_worker, *args, **kwargs
        ):
            return orig_cluster(
                instance,
                dashboard_address=dashboard_address,
                processes=False,
                threads_per_worker=threads_per_worker,
            )

        monkeypatch.setattr(dask.distributed.LocalCluster, "__init__", mock_cluster)

    def ensure_enrollments_mock_run(
        self, monkeypatch, config, static_dataset, temporary_dataset, project_id
    ):
        orig_enrollments = mozanalysis.experiment.Experiment.build_enrollments_query

        def build_enrollments_query_test_project(instance, *args, **kwargs):
            # to use the test project and dataset, we need to change the SQL query
            # generated by mozanalysis
            query = orig_enrollments(instance, *args)
            query = query.replace("moz-fx-data-shared-prod", project_id)
            query = query.replace("telemetry", static_dataset)
            return query

        analysis = Analysis(project_id, temporary_dataset, config)

        monkeypatch.setattr(
            mozanalysis.experiment.Experiment,
            "build_enrollments_query",
            build_enrollments_query_test_project,
        )

        analysis.ensure_enrollments(dt.datetime(2020, 4, 12, tzinfo=pytz.utc))

    def analysis_mock_run(self, monkeypatch, config, static_dataset, temporary_dataset, project_id):
        orig_metrics = mozanalysis.experiment.Experiment.build_metrics_query

        def build_metrics_query_test_project(instance, *args, **kwargs):
            # to use the test project and dataset, we need to change the SQL query
            # generated by mozanalysis
            query = orig_metrics(instance, *args)
            query = query.replace("moz-fx-data-shared-prod", project_id)
            query = query.replace("telemetry", static_dataset)
            return query

        self.mock_local_cluster(monkeypatch)

        analysis = Analysis(project_id, temporary_dataset, config)

        monkeypatch.setattr(
            mozanalysis.experiment.Experiment,
            "build_metrics_query",
            build_metrics_query_test_project,
        )

        analysis.run(dt.datetime(2020, 4, 12, tzinfo=pytz.utc), dry_run=False)

    def test_enrollments(self, monkeypatch, client, project_id, static_dataset, temporary_dataset):
        experiment = Experiment(
            experimenter_slug="test-experiment",
            type="rollout",
            status="Live",
            start_date=dt.datetime(2020, 3, 30, tzinfo=pytz.utc),
            end_date=dt.datetime(2020, 6, 1, tzinfo=pytz.utc),
            proposed_enrollment=7,
            branches=[Branch(slug="branch1", ratio=0.5), Branch(slug="branch2", ratio=0.5)],
            probe_sets=[],
            reference_branch="branch2",
            normandy_slug="test-experiment",
            is_high_population=False,
        )

        config = AnalysisSpec().resolve(experiment)

        test_clients_daily = DataSource(
            name="clients_daily",
            from_expr=f"`{project_id}.test_data.clients_daily`",
        )

        test_active_hours = Metric(
            name="active_hours",
            data_source=test_clients_daily,
            select_expr=agg_sum("active_hours_sum"),
        )

        config.metrics = {AnalysisPeriod.WEEK: [Summary(test_active_hours, BootstrapMean())]}

        self.ensure_enrollments_mock_run(
            monkeypatch, config, static_dataset, temporary_dataset, project_id
        )

        assert (
            client.client.get_table(f"{project_id}.{temporary_dataset}.enrollments_test_experiment")
            is not None
        )

    def test_metrics(self, monkeypatch, client, project_id, static_dataset, temporary_dataset):
        experiment = Experiment(
            experimenter_slug="test-experiment",
            type="rollout",
            status="Live",
            start_date=dt.datetime(2020, 3, 30, tzinfo=pytz.utc),
            end_date=dt.datetime(2020, 6, 1, tzinfo=pytz.utc),
            proposed_enrollment=7,
            branches=[Branch(slug="branch1", ratio=0.5), Branch(slug="branch2", ratio=0.5)],
            probe_sets=[],
            reference_branch="branch2",
            normandy_slug="test-experiment",
            is_high_population=False,
        )

        config = AnalysisSpec().resolve(experiment)

        test_clients_daily = DataSource(
            name="clients_daily",
            from_expr=f"`{project_id}.test_data.clients_daily`",
        )

        test_active_hours = Metric(
            name="active_hours",
            data_source=test_clients_daily,
            select_expr=agg_sum("active_hours_sum"),
        )

        config.metrics = {AnalysisPeriod.WEEK: [Summary(test_active_hours, BootstrapMean())]}

        self.ensure_enrollments_mock_run(
            monkeypatch, config, static_dataset, temporary_dataset, project_id
        )
        self.analysis_mock_run(monkeypatch, config, static_dataset, temporary_dataset, project_id)

        query_job = client.client.query(
            f"""
            SELECT
              *
            FROM `{project_id}.{temporary_dataset}.test_experiment_week_1`
            ORDER BY enrollment_date DESC
        """
        )

        expected_metrics_results = [
            {
                "client_id": "bbbb",
                "branch": "branch2",
                "enrollment_date": datetime.date(2020, 4, 3),
                "num_enrollment_events": 1,
                "analysis_window_start": 0,
                "analysis_window_end": 6,
            },
            {
                "client_id": "aaaa",
                "branch": "branch1",
                "enrollment_date": datetime.date(2020, 4, 2),
                "num_enrollment_events": 1,
                "analysis_window_start": 0,
                "analysis_window_end": 6,
            },
        ]

        r = query_job.result()

        # !! the enrollments table depends on the current run date
        # each enrollment entry has an analysis_window_start and analysis_window_end associated with it
        # analysis_windows_end and _starts are used in the metrics query
        # we would need to make some mozanalysis changes in the queries

        for i, row in enumerate(r):
            print(row)
            for k, v in expected_metrics_results[i].items():
                print(k)
                print(v)
                print(row[k])
                assert row[k] == v

        assert (
            client.client.get_table(f"{project_id}.{temporary_dataset}.test_experiment_weekly")
            is not None
        )
        assert (
            client.client.get_table(
                f"{project_id}.{temporary_dataset}.statistics_test_experiment_week_1"
            )
            is not None
        )

        stats = client.client.list_rows(
            f"{project_id}.{temporary_dataset}.statistics_test_experiment_week_1"
        ).to_dataframe()

        count_by_branch = stats.query("statistic == 'count'").set_index("branch")
        assert count_by_branch.loc["branch1", "point"] == 1.0
        assert count_by_branch.loc["branch2", "point"] == 1.0

        assert (
            client.client.get_table(
                f"{project_id}.{temporary_dataset}.statistics_test_experiment_weekly"
            )
            is not None
        )

    def test_no_enrollments(
        self, monkeypatch, client, project_id, static_dataset, temporary_dataset
    ):
        experiment = Experiment(
            experimenter_slug="test-experiment-2",
            type="rollout",
            status="Live",
            start_date=dt.datetime(2020, 3, 30, tzinfo=pytz.utc),
            end_date=dt.datetime(2020, 6, 1, tzinfo=pytz.utc),
            proposed_enrollment=7,
            branches=[Branch(slug="a", ratio=0.5), Branch(slug="b", ratio=0.5)],
            probe_sets=[],
            reference_branch="a",
            normandy_slug="test-experiment-2",
            is_high_population=False,
        )

        config = AnalysisSpec().resolve(experiment)

        self.ensure_enrollments_mock_run(
            monkeypatch, config, static_dataset, temporary_dataset, project_id
        )
        self.analysis_mock_run(monkeypatch, config, static_dataset, temporary_dataset, project_id)

        query_job = client.client.query(
            f"""
            SELECT
              *
            FROM `{project_id}.{temporary_dataset}.test_experiment_2_week_1`
            ORDER BY enrollment_date DESC
        """
        )

        assert query_job.result().total_rows == 0

        stats = client.client.list_rows(
            f"{project_id}.{temporary_dataset}.statistics_test_experiment_2_week_1"
        ).to_dataframe()

        count_by_branch = stats.query("statistic == 'count'").set_index("branch")
        assert count_by_branch.loc["a", "point"] == 0.0
        assert count_by_branch.loc["b", "point"] == 0.0

        assert (
            client.client.get_table(
                f"{project_id}.{temporary_dataset}.statistics_test_experiment_2_weekly"
            )
            is not None
        )

    # def test_with_segments(
    #     self, monkeypatch, client, project_id, static_dataset, temporary_dataset
    # ):
    #     experiment = Experiment(
    #         experimenter_slug="test-experiment",
    #         type="rollout",
    #         status="Live",
    #         start_date=dt.datetime(2020, 3, 30, tzinfo=pytz.utc),
    #         end_date=dt.datetime(2020, 6, 1, tzinfo=pytz.utc),
    #         proposed_enrollment=7,
    #         branches=[Branch(slug="branch1", ratio=0.5), Branch(slug="branch2", ratio=0.5)],
    #         probe_sets=[],
    #         reference_branch="branch2",
    #         normandy_slug="test-experiment",
    #         is_high_population=False,
    #     )

    #     config = AnalysisSpec().resolve(experiment)

    #     test_clients_daily = DataSource(
    #         name="clients_daily",
    #         from_expr=f"`{project_id}.test_data.clients_daily`",
    #     )

    #     test_active_hours = Metric(
    #         name="active_hours",
    #         data_source=test_clients_daily,
    #         select_expr=agg_sum("active_hours_sum"),
    #     )

    #     test_clients_last_seen = SegmentDataSource(
    #         "clients_last_seen", f"`{project_id}.test_data.clients_last_seen`"
    #     )
    #     regular_user_v3 = Segment(
    #         "regular_user_v3",
    #         test_clients_last_seen,
    #         "COALESCE(LOGICAL_OR(is_regular_user_v3), FALSE)",
    #     )
    #     config.experiment.segments = [regular_user_v3]

    #     config.metrics = {AnalysisPeriod.WEEK: [Summary(test_active_hours, BootstrapMean())]}

    #     self.analysis_mock_run(monkeypatch, config, static_dataset, temporary_dataset, project_id)

    #     query_job = client.client.query(
    #         f"""
    #         SELECT
    #           *
    #         FROM `{project_id}.{temporary_dataset}.test_experiment_week_1`
    #         ORDER BY enrollment_date DESC
    #     """
    #     )

    #     expected_metrics_results = [
    #         {
    #             "client_id": "bbbb",
    #             "branch": "branch2",
    #             "enrollment_date": datetime.date(2020, 4, 3),
    #             "num_enrollment_events": 1,
    #             "analysis_window_start": 0,
    #             "analysis_window_end": 6,
    #             "regular_user_v3": True,
    #         },
    #         {
    #             "client_id": "aaaa",
    #             "branch": "branch1",
    #             "enrollment_date": datetime.date(2020, 4, 2),
    #             "num_enrollment_events": 1,
    #             "analysis_window_start": 0,
    #             "analysis_window_end": 6,
    #             "regular_user_v3": False,
    #         },
    #     ]

    #     for i, row in enumerate(query_job.result()):
    #         for k, v in expected_metrics_results[i].items():
    #             assert row[k] == v

    #     assert (
    #         client.client.get_table(f"{project_id}.{temporary_dataset}.test_experiment_weekly")
    #         is not None
    #     )
    #     assert (
    #         client.client.get_table(
    #             f"{project_id}.{temporary_dataset}.statistics_test_experiment_week_1"
    #         )
    #         is not None
    #     )

    #     stats = client.client.list_rows(
    #         f"{project_id}.{temporary_dataset}.statistics_test_experiment_week_1"
    #     ).to_dataframe()

    #     count_by_branch = stats.query("segment == 'all' and statistic == 'count'").set_index(
    #         "branch"
    #     )
    #     assert count_by_branch.loc["branch1", "point"] == 1.0
    #     assert count_by_branch.loc["branch2", "point"] == 1.0

    #     assert len(stats.query("segment == 'regular_user_v3'")) > 0

    #     assert (
    #         client.client.get_table(
    #             f"{project_id}.{temporary_dataset}.statistics_test_experiment_weekly"
    #         )
    #         is not None
    #     )
