### Payrexx Integration

Payrexx payment gateway integration for the Frappe payments app

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app payrexx_integration
```

### Configuration

See [`HOW_TO.md`](HOW_TO.md) for gateway, webhook, and Payment Gateway Account
setup. Custom Payrexx Platform API domains are denied unless their exact final
host is listed in the site's `payrexx_allowed_api_hosts` JSON array; canonical
Payrexx-owned hosts work without an override.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/payrexx_integration
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

unlicense
