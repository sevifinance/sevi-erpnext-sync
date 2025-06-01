### Sevi Plugin

> [!WARNING]
> Do not merge this branch into the main branch.

The Sevi Plugin is used to synchronize external systems, such as payment information, with ERPNext via the REST API.

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/sevifinance/erpnext.git --branch sevi
bench install-app sevi
```

> [!CAUTION]
> Because the repository is currently named `erpnext`, it will be cloned into a folder named `erpnext` by default, which may conflict with an existing `erpnext` directory in your app folder. 👉 To avoid this conflict, this app should have its own separate repository named `sevi`. 

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/sevi
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade
### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

unlicense
