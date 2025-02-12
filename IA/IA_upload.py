import os
import argparse
import requests
import boto
from boto.s3.connection import OrdinaryCallingFormat
from boto.s3.multipart import MultiPartUpload
import asyncio
import xmltodict
from io import BytesIO
from IA.utils import put_with_retry, sleep_and_retry_on
from settings import (
    CHUNK_SIZE,
    OSF_COLLECTION_NAME,
    IA_ACCESS_KEY,
    IA_SECRET_KEY,
    IA_URL,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def mp_from_ids(mp_id: str, mp_keyname: str, bucket: str) -> MultiPartUpload:
    mp = MultiPartUpload(bucket)
    mp.key_name = mp_keyname
    mp.id = mp_id
    return mp


async def gather_and_upload(bucket_name: str, parent: str):
    '''
    This script traverses through a directory uploading everything in it to Internet Archive.
    '''

    tasks = []

    directory_path = os.path.join(HERE, parent)
    for root, dirs, files in os.walk(directory_path):
        for file in files:
            path = os.path.join(root, file)
            with open(path, 'rb') as fp:
                data = fp.read()
                size = len(data)
                url_path = path.split(parent+'/')[1]
                if size > CHUNK_SIZE:
                    tasks.append(chunked_upload(bucket_name, url_path, data))
                else:
                    tasks.append(upload(bucket_name, url_path, data))

    await asyncio.gather(*tasks)


async def upload(bucket_name: str, filename: str, file_content: bytes):
    headers = {
        'authorization': 'LOW {}:{}'.format(IA_ACCESS_KEY, IA_SECRET_KEY),
        'x-amz-auto-make-bucket': '1',
        'Content-Type': 'application/octet-stream',
        'x-archive-meta01-collection': OSF_COLLECTION_NAME,
    }
    url = f'{IA_URL}/{bucket_name}/{filename}'
    resp = put_with_retry(f'http://{url}', headers=headers, data=file_content, retry_on=(429, 503))

    if resp.status_code != 200:
        error_json = dict(xmltodict.parse(resp.content))
        raise requests.exceptions.HTTPError(error_json)


async def chunked_upload(bucket_name: str, filename: str, file_content: bytes):
    conn = boto.connect_s3(
        IA_ACCESS_KEY,
        IA_SECRET_KEY,
        host=IA_URL,
        is_secure=False,
        calling_format=OrdinaryCallingFormat(),
    )
    bucket = conn.lookup(bucket_name)
    if bucket is None:
        bucket = conn.create_bucket(
            bucket_name,
            headers={'x-archive-meta01-collection': OSF_COLLECTION_NAME}
        )

    initiate_multipart_with_retry = sleep_and_retry_on(
        bucket.initiate_multipart_upload,
        retry_on=boto.exception.S3ResponseError
    )

    mp = initiate_multipart_with_retry(filename)

    tasks = []
    chunks = [file_content[i:i + CHUNK_SIZE] for i in range(0, len(file_content), CHUNK_SIZE)]

    for i, chunk in enumerate(chunks):
        mp = mp_from_ids(mp.id, filename, bucket)
        upload_part_from_file = asyncio.coroutine(mp.upload_part_from_file)
        tasks.append(asyncio.ensure_future(upload_part_from_file(BytesIO(chunk), i + 1)))

    await asyncio.gather(*tasks)

    mp.complete_upload()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-b',
        '--bucket',
        help='The name of the bucket you want to dump in.',
        required=True,
    )
    parser.add_argument(
        '-s',
        '--source',
        help='The name of the folder you want to dump.',
        required=True,
    )
    args = parser.parse_args()
    bucket = args.bucket
    source = args.source

    asyncio.run(gather_and_upload(bucket, source))
