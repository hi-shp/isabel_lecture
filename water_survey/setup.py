from setuptools import setup, find_packages
from glob import glob
setup(name='water_survey', version='1.0.0', packages=find_packages(),
      package_data={'water_survey':['static/*','static/vendor/*','static/vendor/images/*']},
      data_files=[('share/ament_index/resource_index/packages',['resource/water_survey']),
                  ('share/water_survey',['package.xml','THIRD_PARTY_NOTICES.txt']),
                  ('share/water_survey/launch',glob('launch/*.launch.py')),
                  ('share/water_survey/config',glob('config/*.yaml')),
                  ('share/water_survey/scripts',['scripts/stop_duplicate_bringup.py','scripts/configure_ntrip_seed.py'])],
      install_requires=['setuptools'], zip_safe=False,
      maintainer='Boat team', maintainer_email='boat@example.invalid',
      description='DO waypoint survey dashboard', license='MIT',
      entry_points={'console_scripts':['survey_dashboard = water_survey.node:main',
                                      'survey_rebuild = water_survey.replay:main']})
