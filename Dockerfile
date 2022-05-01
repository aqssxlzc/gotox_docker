# syntax=docker/dockerfile:1

FROM python:3.8-slim-buster

WORKDIR /app

COPY GotoX-3.6.2-cp38-win_amd64 GotoX-3.6.2-cp38-win_amd64
RUN pip3 install -r /app/GotoX-3.6.2-cp38-win_amd64/requirements.txt

COPY start.sh .
ENTRYPOINT [ "/bin/bash","start.sh"]

