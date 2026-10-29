# Proposal: Lemonseed Modules Framework

The Lemonseed AI appliance is extensible to meet user needs through the use of *Modules*, which provide additional functionality on top of the core control plane, Lemonade-server, and Open-WebUI platform. Modules will have a *declared type* which will define how they are presented to the user, integrated into the platform, and sandboxed.

## Module types
We expect to start with 2-3 declared module types, but we reserve the right to add additional types in the future.

| Type | Description |
| ---- | ----------- |
| Application | A web based application that is built on top of the core AI foundation. Users would typically interact directly with this application via the Lemonseed web interface; the application would be placed behind our NGINX proxy. |
| Plugin | These provide a backend capability that can be used by Open-WebUI or other applications. The most common form of plugin would be an MCP endpoint, but we could also build dataset managers (e.g. Wikipedia) or other elements.
| Connection | Extend the network connectivity of the Lemonseed appliance; the most common example would be a VPN technology such as Tailscale / Headscale or CloudflareD. This is where our future "Cloud" product could plug in. Network egress would also be supported, in case users wanted to enable an outbound PROXY server or public VPN for traffic anonymization. |

*Open question: should additional LLMs outside of the Lemonseed base catalog be offered as Modules? Or should we build a separate system for our expanded "LLM Catalog"?*

## Requirements

This is an (incomplete) list of requirements for the module specification and implementation. We will add more requirements to this list as we discover gaps in our implementation.

1. Modules are defined by a revision controlled YAML fragment in the Lemonseed repository; they may also include a directory of supporting files (scripts, logos, etc).
1. Modules must declare a globally unique ID, name, license, maturity (alpha/beta/production), and version
1. Modules must declare one or more owners (as github user IDs? GH issue targets?) so that we can contact owners / assign GH issues when problems arise
1. Modules may also provide a logo image, plain text description, and link to homepage
1. Modules must declare a module type (see above)
1. Modules may declare a lemonseed CP control panel element to enable users to configure parameters for the module; any settings exposed will be passed into the module via environment variables.
1. Modules must declare a spec version; we will start with v1 as a published spec schema. If we need to make breaking changes, we will introduce v2 while maintaining backward compatibility with v1 based modules.
1. Modules must be revision controlled in the Lemonseed github; we will not support external module catalogs (at least for now).
1. Modules must be sandboxed on the Lemonseed appliance, typically via Docker containers, but for the `Connection` type we may introduce other ways to sandbox a module (or allow containers with expanded capabilities).
1. Modules may declare dependencies on core system offerings (e.g. a given module needs Valkey) or other modules (e.g. a given Application module needs an MCP module to be installed). Models may also declare dependencies on specific AI capabilities (e.g. Stable Diffusion) or LLM model families (e.g. Gemma).
1. Modules may include scripts with execution hooks to enable specific actions to take place on the base Lemonseed (scripts do not run inside the Docker container). Initial script hooks should include:
   * Module install
   * Module remove
   * Module start
   * Module stop
   * User CRUD event
1. If modules depend on the system Postgres or Valkey, they must provide schema and other parameters to define the database that will be created for them. Lemonseed will then provide the URI and credentials to the database as environment variables to the module docker comtainer.
1. Modules may require 2+ Docker containers by specifying a `docker-compose.yml` service fragment, which Lemonseed will integrate and manage **(this can be backlogged until we need it)**
1. Modules must declare any outbound connections or external "cloud" dependencies they have; for example a GMail module would declare a cloud dependency on Google Mail.
1. Modules must provide a basic test harness that does not have external dependencies to enable Lemonseed to test all modules against new Lemonseed releases.

## Module Catalog &amp; Management

The user will be able to browse and search the Lemonseed module catalog. To ensure that the catalog stays fresh, it will be hosted online and refreshed by the Lemonseed CP periodically. A few notes on the implementation of the Module Catalog:

* The CP will always operate on a local copy of the module catalog; we will not operate a "hosted" model where the catalog is provided as a web service. Catalog downloads will be time fuzzed to avoid upstream overload.
* The module catalog presentation will be filtered to only share modules which are compatible with the user Lemonseed (hardware, version, etc).
* The module catalog will be organized by module type so that users can search for apps, plugins, etc. Each type should basically be a tab within the module catalog page.
* The module installer will automatically install declared dependencies (e.g. apps that depend on plugins), ensuring that all dependencies can be met before starting the install
* Modules can always be uninstalled (and will check / manage dependencies when this happens as well)
* The module catalog can defined "Featured" modules that will be highlighted to the user at the top of the catalog page, helping users find the most important or useful modules quickly
* Modules that stop working or become flakey will be shut off and surfaced to the user as broken modules; users can choose to reinstall, reset (settings, database), or remove these modules.
