import glob
import os

from setuptools import setup

PACKAGE_NAME = 'segtask_server'

setup(
    name=PACKAGE_NAME,
    version='1.0.0',
    packages=[PACKAGE_NAME, f'{PACKAGE_NAME}.clients'],
    data_files=[
        ('share/ament_index/resource_index/packages', [f'resource/{PACKAGE_NAME}']),
        (f'share/{PACKAGE_NAME}', ['package.xml']),
        (f'lib/{PACKAGE_NAME}', glob.glob('scripts/*.py')),
        (f'share/{PACKAGE_NAME}/examples', glob.glob('examples/*.json')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='segtask',
    maintainer_email='dev@example.com',
    description='Segmented task action server with cancel/terminal-state consistency and SQLite persistence',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'segtask_server_node = segtask_server.cli:main',
            'segtask_audit = segtask_server.audit:main',
        ],
    },
)
