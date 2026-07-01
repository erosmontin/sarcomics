docker run -it \
  --name radiomics-live-test \
  -v "$PWD":/workspace \
  -w /workspace \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  ubuntu:22.04 \
  bash