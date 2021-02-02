import datetime as dt

import pytest
import pytz

from jetstream.experimenter import Branch, Experiment


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        help="Run integration tests",
    )


@pytest.fixture
def experiments():
    return [
        Experiment(
            experimenter_slug="test_slug",
            type="pref",
            status="Complete",
            start_date=dt.datetime(2019, 12, 1, tzinfo=pytz.utc),
            end_date=dt.datetime(2020, 3, 1, tzinfo=pytz.utc),
            proposed_enrollment=7,
            branches=[Branch(slug="a", ratio=1), Branch(slug="b", ratio=1)],
            normandy_slug="normandy-test-slug",
            reference_branch="b",
            is_high_population=False,
            app_name="firefox_desktop",
            app_id="firefox-desktop",
        ),
        Experiment(
            experimenter_slug="test_slug",
            type="addon",
            status="Complete",
            start_date=dt.datetime(2019, 12, 1, tzinfo=pytz.utc),
            end_date=dt.datetime(2020, 3, 1, tzinfo=pytz.utc),
            proposed_enrollment=0,
            branches=[],
            normandy_slug=None,
            reference_branch=None,
            is_high_population=False,
            app_name="firefox_desktop",
            app_id="firefox-desktop",
        ),
        Experiment(
            experimenter_slug="test_slug",
            type="pref",
            status="Live",
            start_date=dt.datetime(2019, 12, 1, tzinfo=pytz.utc),
            end_date=dt.datetime(2020, 3, 1, tzinfo=pytz.utc),
            proposed_enrollment=7,
            branches=[],
            normandy_slug="normandy-test-slug",
            reference_branch=None,
            is_high_population=False,
            app_name="firefox_desktop",
            app_id="firefox-desktop",
        ),
        Experiment(
            experimenter_slug="test_slug",
            type="pref",
            status="Live",
            start_date=dt.datetime(2019, 12, 1, tzinfo=pytz.utc),
            end_date=dt.datetime(2020, 3, 1, tzinfo=pytz.utc),
            proposed_enrollment=7,
            branches=[],
            normandy_slug="normandy-test-slug",
            reference_branch=None,
            is_high_population=True,
            app_name="firefox_desktop",
            app_id="firefox-desktop",
        ),
        Experiment(
            experimenter_slug="test_slug",
            type="pref",
            status="Complete",
            start_date=dt.datetime(2019, 12, 1, tzinfo=pytz.utc),
            end_date=dt.datetime(2020, 3, 1, tzinfo=pytz.utc),
            proposed_enrollment=7,
            branches=[Branch(slug="a", ratio=1), Branch(slug="b", ratio=1)],
            normandy_slug="normandy-test-slug",
            reference_branch="b",
            is_high_population=False,
            app_name="firefox_desktop",
            app_id="firefox-desktop",
        ),
        Experiment(
            experimenter_slug="test_slug",
            type="pref",
            status="Live",
            start_date=dt.datetime(2019, 12, 1, tzinfo=pytz.utc),
            end_date=dt.datetime(2020, 3, 1, tzinfo=pytz.utc),
            proposed_enrollment=7,
            branches=[],
            probe_sets=[],
            normandy_slug="normandy-test-slug",
            reference_branch=None,
            is_high_population=True,
            outcomes=["performance", "tastiness"],
            app_name="firefox_desktop",
            app_id="firefox-desktop",
        ),
    ]


@pytest.fixture
def fenix_experiments():
    return [
        Experiment(
            experimenter_slug="my_fenix_experiment",
            normandy_slug="my_fenix_experiment",
            type="v6",
            status="Live",
            branches=[Branch(slug="foo", ratio=1), Branch(slug="bar", ratio=1)],
            start_date=dt.datetime(2020, 1, 1, tzinfo=pytz.UTC),
            end_date=dt.datetime(2020, 10, 10, tzinfo=pytz.UTC),
            proposed_enrollment=7,
            reference_branch="foo",
            app_name="fenix",
            app_id="org.mozilla.firefox",
            is_high_population=False,
        ),
        Experiment(
            experimenter_slug="my_fenix_nightly_experiment",
            normandy_slug="my_fenix_nightly_experiment",
            type="v6",
            status="Live",
            branches=[Branch(slug="foo", ratio=1), Branch(slug="bar", ratio=1)],
            start_date=dt.datetime(2020, 1, 1, tzinfo=pytz.UTC),
            end_date=dt.datetime(2020, 10, 10, tzinfo=pytz.UTC),
            proposed_enrollment=7,
            reference_branch="foo",
            app_name="fenix",
            app_id="org.mozilla.fenix",
            is_high_population=False,
        ),
    ]
