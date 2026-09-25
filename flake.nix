{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
  };
  outputs = {nixpkgs, ...}: let
    forAllSystems = function:
      nixpkgs.lib.genAttrs nixpkgs.lib.systems.flakeExposed (
        system: function nixpkgs.legacyPackages.${system}
      );
  in {
    formatter = forAllSystems (pkgs: pkgs.alejandra);
    devShells = forAllSystems (pkgs: {
      default = pkgs.mkShell {
        packages = with pkgs; [
          nodejs
          python314
          uv
        ];
        env = {
          UV_PYTHON = "${pkgs.python314}/bin/python3";
          UV_PYTHON_DOWNLOADS = "never";
        };
        # Hermes' own lockfile pins the runtime the plugin loads into.
        shellHook = ''
          export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
          uv sync --quiet --frozen --project vendor/hermes --extra discord --group dev
          source .venv/bin/activate
        '';
      };
    });
  };
}
