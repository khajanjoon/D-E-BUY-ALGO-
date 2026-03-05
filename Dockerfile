FROM python:3.11-alpine
LABEL maintainer="ksjoon1234@gmail.com"
COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt
ENTRYPOINT ["python"]
CMD ["src/app.py"]
