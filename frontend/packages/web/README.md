# Development

The web crate defines the entrypoint for the web app along with any assets, components and dependencies that are specific to web builds. The web crate starts out something like this:

```
web/
├─ assets/ # Assets used by the web app - Any platform specific assets should go in this folder
├─ src/
│  ├─ main.rs # The entrypoint for the web app.It also defines the routes for the web platform
│  ├─ views/ # The views each route will render in the web version of the app
│  │  ├─ mod.rs # Defines the module for the views route and re-exports the components for each route
│  │  ├─ blog.rs # The component that will render at the /blog/:id route
│  │  ├─ home.rs # The component that will render at the / route
├─ Cargo.toml # The web crate's Cargo.toml - This should include all web specific dependencies
```

## Dependencies
This crate will only be included in the web build, so you should add all web specific dependencies to this crate's [Cargo.toml](../Cargo.toml) file instead of the shared [ui](../ui/Cargo.toml) crate.

### Serving Your Web App

You can start your web app with the following command:

```bash
dx serve
```

## Configuration

Settings are read from the environment at **build time** and compiled into
the WASM binary (a browser app has no runtime environment). All of them are
in [`src/config.rs`](src/config.rs):

| Variable             | Debug builds (`dx serve`)          | Release builds (`dx build --release`) |
|----------------------|------------------------------------|---------------------------------------|
| `STASH_API_BASE_URL` | optional, default `http://localhost:8000` | **required**; the build fails without it |
| `STASH_TURNSTILE_SITE_KEY` | optional, default Cloudflare's test key | **required** (empty: no widget) |
| `STASH_ANALYTICS_ENABLED` | optional; `true` turns product analytics on | same |
| `STASH_POSTHOG_PROJECT_API_KEY` | the PostHog project API key (`phc_...`) | same |
| `STASH_POSTHOG_HOST` | e.g. `https://eu.i.posthog.com` | same |

Analytics is off unless all three analytics variables are set (see
[docs/deployment.md](../../../docs/deployment.md#product-analytics-posthog));
an invalid key or host fails the build.

The value is the backend API's base URL: `http://` or `https://`, no
trailing slash (e.g. Terraform's `api_url` output). Invalid values fail the
build too. Changing it triggers a rebuild.

### Production build

From `frontend/packages/web`:

```bash
STASH_API_BASE_URL="$(terraform -chdir=../../../infra/terraform/live output -raw api_url)" \
  dx build --platform web --release
```

The static site is `frontend/target/dx/web/release/web/public/`. dx doesn't
clear that directory between builds, so remove it first when its contents
are deployed as a whole.
