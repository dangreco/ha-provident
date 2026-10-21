# SPDX-License-Identifier: GPL-3.0-only

{
  description = "dangreco/ha-provident";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
    git-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      imports = [ inputs.git-hooks.flakeModule ];

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      perSystem =
        {
          config,
          lib,
          pkgs,
          system,
          ...
        }:
        let
          # git-hooks.nix + flake-parts recurse unless `pkgs` is provided externally.
          pkgs' = import inputs.nixpkgs { inherit system; };

          # HA 2026.10 needs Python >= 3.14.2.
          python = pkgs.python314;

        in
        {
          _module.args.pkgs = pkgs';

          pre-commit.settings.hooks = {
            nixfmt.enable = true;
            nil.enable = true;
            statix.enable = true;
            deadnix.enable = true;
            flake-checker.enable = true;

            ruff.enable = true;
            ruff-format.enable = true;
            check-json.enable = true;
            check-toml.enable = true;

            typos.enable = true;
            commitizen.enable = true;
            actionlint.enable = true;
            zizmor = {
              enable = true;
              files = "^\\.github/(workflows|actions)/";
              # Offline so the hook is deterministic.
              entry = "${pkgs.zizmor}/bin/zizmor --offline";
            };
            pinact = {
              enable = true;
              name = "pinact";
              description = "Ensure GitHub Actions are pinned to full commit SHAs";
              entry = "${pkgs.pinact}/bin/pinact run --check";
              files = "^\\.github/(workflows|actions)/";
              types = [ "yaml" ];
              pass_filenames = false;
            };
            check-merge-conflicts.enable = true;
            check-added-large-files = {
              enable = true;
              # uv.lock is large and committed on purpose.
              excludes = [ "^uv\\.lock$" ];
            };
            check-case-conflicts.enable = true;
            check-symlinks.enable = true;
            check-executables-have-shebangs.enable = true;
            check-shebang-scripts-are-executable.enable = true;
            forbid-new-submodules.enable = true;
            fix-byte-order-marker.enable = true;
            mixed-line-endings.enable = true;
            end-of-file-fixer.enable = true;
            trim-trailing-whitespace.enable = true;
            detect-private-keys.enable = true;
            ripsecrets.enable = true;

          };

          devShells = {
            default = pkgs.mkShellNoCC {
              inherit (config.pre-commit) shellHook;
              # Other interpreters may shadow `python3` on PATH.
              PYTHON = lib.getExe python;

              packages =
                with pkgs;
                [
                  nil
                  nixfmt
                  gnumake
                ]
                ++ config.pre-commit.settings.enabledPackages;

              nativeBuildInputs = with pkgs; [
                python
                uv
              ];
            };

            # CI shell: no git-hooks shellHook, which would install hooks.
            ci = pkgs.mkShellNoCC {
              PRE_COMMIT_CONFIG = config.pre-commit.settings.configFile;
              PYTHON = lib.getExe python;

              packages = [
                config.pre-commit.settings.package
                pkgs.gnumake
                pkgs.uv
                python
              ]
              ++ config.pre-commit.settings.enabledPackages;
            };
          };
        };
    };
}
