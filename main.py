import json
import os
import re
import signal
import subprocess
import boto3
import requests
from yt_dlp import YoutubeDL
import pika

from dotenv import load_dotenv

regex_url = r'^https:\/\/[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(:[0-9]{1,5})?(\/.*)?$'
regex_uuid = r'^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[1-5][a-fA-F0-9]{3}-[89abAB][a-fA-F0-9]{3}-[a-fA-F0-9]{12}$'
accepted_file_types = ['mp4', 'webm', 'mov', 'mp3', 'aac', 'flac', 'wav', 'm4a', 'ogg', 'wma', 'opus']

if os.getenv('ENV') == 'production':
    load_dotenv(".env.production.local")
else:
    load_dotenv(".env.development.local")

AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.getenv('AWS_REGION')
AWS_S3_BUCKET_NAME = os.getenv('AWS_S3_BUCKET_NAME')
AWS_S3_STATIC_PAGE_URL = os.getenv('AWS_S3_STATIC_PAGE_URL')

params = pika.ConnectionParameters(
    host=os.getenv('RABBITMQ_HOST'),
    port=int(os.getenv('RABBITMQ_PORT')),
    credentials=pika.PlainCredentials(
        username=os.getenv('RABBITMQ_USER'),
        password=os.getenv('RABBITMQ_PASSWORD')
    )
)


s3_client = boto3.client(
    's3',
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)


def download_yt(url: str, uuid: str):
    yt_opts = {
        'quiet': True,
        'format': 'bestaudio/best',
        'outtmpl': f'{uuid}.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
    }
    
    if os.path.exists('cookies.txt'):
        yt_opts['cookiefile'] = 'cookies.txt'
    
    try:
        with YoutubeDL(yt_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(e)
        
        
def has_audio(file: str):
    try:
        result = subprocess.run(
            ['ffprobe', '-i', file],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        return 'Audio' in str(result.stdout)
    except Exception as e:
        print(e)
        return False        
        
    
def download_file(url: str, uuid: str):
    if not any([file_type in url for file_type in accepted_file_types]):
        print('Invalid file type')
        return
    
    try:
        response = requests.get(url)
        with open(f'{uuid}', 'wb') as f:
            f.write(response.content)
    except Exception as e:
        print(e)
        return
    
    if not has_audio(uuid):
        print('No audio found')
    else:
        os.system(f'ffmpeg -hide_banner -loglevel error -y -i {uuid} -vn -ar 48000 -ac 2 -b:a 320k {uuid}.mp3')
    
    if os.path.exists(uuid):
        os.remove(uuid)
        
        
def upload_to_S3(folder: str, file: str):
    print(f'Uploading {file} to S3')
    try:
        s3_client.upload_file(
            file,
            AWS_S3_BUCKET_NAME,
            f'{folder}/{file}'
        )
        print(f'{AWS_S3_STATIC_PAGE_URL}/{folder}/{file}')
    except Exception as e:
        print(e)
    

def download_send(url: str, uuid: str, folder: str):
    try: 
        if "youtu" in url:
            download_yt(url, uuid)
        else:
            download_file(url, uuid)
            
        upload_to_S3(folder, f'{uuid}.mp3')
    except Exception as e:
        print(e)
    finally:
        if os.path.exists(f'{uuid}.mp3'):
            os.remove(f'{uuid}.mp3')
            
def handle(body):
    if not body:
        raise ValueError('No body received')
    data = body.decode()

    if not data:
        raise ValueError('No data received')
    if not isinstance(data, str):
        raise ValueError('Data is not a string')
    
    try:
        json_data = json.loads(data)
    except json.JSONDecodeError:
        raise ValueError('Invalid JSON format')
    
    required_fields = {'url': 'URL is required', 'folder': 'Folder is required', 'uuid': 'UUID is required'}
    for field, error_message in required_fields.items():
        if not json_data.get(field):
            raise ValueError(error_message)
        
    url = json_data['url']
    if re.match(regex_url, url) is None:
        raise ValueError(f'Invalid URL: {url}')

    uuid = json_data['uuid']
    if re.match(regex_uuid, uuid) is None:
        raise ValueError(f'Invalid UUID: {uuid}')

    folder = json_data['folder']
    
    print(f'Processing {url} with UUID {uuid} in folder {folder}')
    download_send(url, uuid, folder)
    print(f'Processed data: {uuid}')
    
    
def callback(ch, method, properties, body):
    print('Received message')
    try:
        handle(body)
    except Exception as e:
        print(f'Error processing message: {e}')
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return
    
    ch.basic_ack(delivery_tag=method.delivery_tag)
    

def graceful_shutdown(signum, frame):
    print(f'Received signal {signum}, shutting down gracefully...')
    try:
        channel.stop_consuming()
        channel.close()
        connection.close()
        print('Connection closed')
    except Exception as e:
        print(f'Error during shutdown: {e}')
    exit(0)


if __name__ == '__main__':
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.queue_declare(queue='vts3a_convert_queue', durable=True)
    
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue='vts3a_convert_queue', on_message_callback=callback)
    
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    
    try:
        print('Waiting for messages. To exit press CTRL+C')
        channel.start_consuming()
    except KeyboardInterrupt:
        print('Exiting...')
        channel.stop_consuming()
        channel.close()
        connection.close()
        print('Connection closed')
        print('Exiting...')
        exit(0)
