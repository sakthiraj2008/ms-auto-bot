FROM python:3.10-slim-bookworm

RUN apt update -o Acquire::Check-Valid-Until=false && apt install -y git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /requirements.txt
RUN pip3 install -U pip && pip3 install -U -r /requirements.txt

RUN mkdir /app
WORKDIR /app
COPY . /app

CMD ["python3", "bot.py"]
