FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm
# The image ships a Yarn apt source whose signing key has expired; it breaks the
# apt-get update that the docker-in-docker feature runs. Nothing here needs Yarn.
RUN rm -f /etc/apt/sources.list.d/yarn.list
