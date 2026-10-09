# Unofficial Python SDK for the Lemonade (lemond) HTTP API.
#
# Not in nixpkgs as of nixos-26.05. Pure-Python wheel; the only runtime
# dependency is `requests`. The control plane uses it (via a thin subclass in
# control-plane/lemond_client.py) to talk to lemond over HTTP instead of
# wrapping the `lemonade` CLI — see DESIGN.md "lemond API over CLI".
{
  lib,
  fetchurl,
  buildPythonPackage,
  requests,
  setuptools,
}:

buildPythonPackage {
  pname = "lemonade-python-sdk";
  version = "1.0.10";
  pyproject = true;

  # Sdist from files.pythonhosted.org (the pypi.org "packages/source" URLs 404;
  # only the files.pythonhosted.org path is stable and fetchable). The sdist
  # unpacks natively; a .whl would not be recognised by the stdenv unpacker.
  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/source/l/lemonade-python-sdk/lemonade_python_sdk-1.0.10.tar.gz";
    sha256 = "000b33f2f804d30b3f2f4df851bb768c8937c645bc833998dacfcd4047ceb1be";
  };

  # setuptools is needed to build the wheel from the sdist (build backend is
  # setuptools.build_meta). Only `requests` is a runtime dependency; the rest
  # are the `dev` extra (pytest, pytest-cov, flake8) which we do not need.
  nativeBuildInputs = [ setuptools ];
  propagatedBuildInputs = [ requests ];

  meta = with lib; {
    description = "Python SDK for the Lemonade LLM server HTTP API (unofficial)";
    homepage = "https://pypi.org/project/lemonade-python-sdk/";
    license = licenses.mit;
    platforms = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
  };
}
