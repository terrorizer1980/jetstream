[![CircleCI](https://circleci.com/gh/mozilla/jetstream/tree/master.svg?style=shield)](https://circleci.com/gh/mozilla/jetstream/tree/master)

# jetstream

Automated experiment analysis.

Jetstream automatically calculates metrics and applies statistical treatments to collected experiment data for different analysis windows.

For more information, see [the documentation](https://github.com/mozilla/jetstream/wiki).

## Running tests

Make sure `tox` is installed globally (run `brew install tox` or `pip install tox`).

Then, run `tox` from wherever you cloned this repository. (You don't need to install jetstream first.)

To run integration tests, run `tox -e py38-integration`.


## Local installation

```bash
# Create and activate a python virtual environment.
python3 -m venv venv/
source venv/bin/activate
pip install -r requirements.txt
```
