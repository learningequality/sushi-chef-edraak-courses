#!/usr/bin/env python
from bs4 import BeautifulSoup
from bs4.element import NavigableString
import json
from html2text import html2text
import os
from PIL import Image
import re
import requests
from urllib.parse import urljoin

from le_utils.constants import content_kinds, exercises, file_types, licenses, roles
from le_utils.constants.languages import getlang  # see also getlang_by_name, getlang_by_alpha2
from ricecooker.chefs import JsonTreeChef
from ricecooker.classes.licenses import get_license
from ricecooker.utils.jsontrees import write_tree_to_json_tree

from ricecooker.config import LOGGER
import logging
LOGGER.setLevel(logging.INFO)


from libedx import extract_course_tree


DEBUG_MODE = True




# EDRAAK CONSTANTS
################################################################################
EDRAAK_COURSES_DOMAIN = 'edraak.org'
EDRAAK_COURSES_CHANNEL_DESCRIPTION = """Sample courses from the Edraak contrinuing education selection."""
EDRAAK_LICENSE = get_license(licenses.CC_BY_NC_SA, copyright_holder='Edraak').as_dict()

COURSES_DIR = 'chefdata/Courses'
SKIP_KINDS = ['wiki']   # edX content kinds not suported in Kolibri
EDRAAK_STRINGS = {
    "learning_objectives": [
        'الأهداف التعليمية',
        'الاهداف التعليمية'
    ],
    "knowledge_check": 'التحقق من المعرفة',
}
TITLES_TO_DROP = [
    'التسجيل في المساق',       # Registration in the course
    'كيفية استخدام المنصة',    # How to use the platform
    'توضيح الايقونات',          # Illustration icons
    'استخدام الالة الحاسبة',    # Use the calculator
    'محاكي الاختبار',           # Test Simulator = downloadable .exe
    'كيفية إصدار الشهادة',     # How to issue the certificate
    'الإنهاء من المساق',        # Termination of the course
    'إستبيان التسجيل في المساق',            # Course Registration Questionnaire
    'تابعونا على مواقع التواصل الاجتماعي',   # Follow us on social media
    'إستبيان الانتهاء من المساق',  # Course completion questionnaire
    'استبيان نهاية المساق',       # End of course questionnaire
    
]





# TREE FUNCTIONS FOR PARSING EDRAAK COURSES
################################################################################

def guess_vertical_type(vertical):
    children_kinds = set([child['kind'] for child in vertical['children']])
    if 'discussion' in children_kinds:
        return 'discussion_vertical'
    elif 'video' in children_kinds:
        return 'video_vertical'
    elif 'problem' in children_kinds and 'video' not in children_kinds:
        return 'test_vertical'

    title = vertical.get('display_name')
    if title and any(lo in title for lo in EDRAAK_STRINGS['learning_objectives']):
        return 'learning_objectives_vertical'

    if children_kinds == set(['html']):
        return 'html_vertical'

    return None


def clean_subtree(subtree):
    kind = subtree['kind']
    if kind == 'video':
        subtree = process_video(subtree)

    # Filter children
    new_children = []
    if 'children' in subtree:
        for child in subtree['children']:
            child_kind = child['kind']

            # 1. DROP BASED ON KIND
            if child_kind in SKIP_KINDS:
                continue

            # 2. DROP BASED ON TITLE
            child_title = child['display_name'] if 'display_name' in child else ''
            if any(t in child_title for t in TITLES_TO_DROP):
                continue
            
            # 3. DROP BASED ON vertical_type
            if child_kind == 'vertical':
                vertical_type = guess_vertical_type(child)
                if vertical_type is None:
                    from libedx import print_course
                    print_course(child, translate_from='ar')
                    raise ValueError('unrecognized vertical type...')

            # Recurse
            clean_child = clean_subtree(child)
            new_children.append(clean_child)
    subtree['children'] = new_children

    return subtree


def process_video(video):
    """
    Extracts the `youtube_id` or `path` link from the XML in video['content'].
    """
    xml = video['content']
    doc = BeautifulSoup(xml, "xml")

    # CASE A
    encoded_video = doc.find('encoded_video', {'profile': "youtube"})
    if encoded_video:
        video['youtube_id'] = encoded_video['url']
        return video

    # CASE B
    video_source = doc.find('source')
    if video_source:
        video['path'] = video_source['src']
        return video

    # CASE C
    video_el = doc.find('video')
    if video_el:
        youtube_id = video_el.get('youtube_id_1_0')
        if youtube_id:
            video['youtube_id'] = youtube_id
            return video

    raise ValueError('Unrecognized video format encountered')



































# CHEF
################################################################################

class EdraakCoursesChef(JsonTreeChef):
    """
    The chef class that takes care of uploading channel to Kolibri Studio.
    We'll call its `main()` method from the command line script.
    """
    RICECOOKER_JSON_TREE = 'edraak_courses_ricecooker_json_tree.json'


    def add_content_nodes(self, channel):
        """
        Build the hierarchy of topic nodes and content nodes.
        """
        LOGGER.info('Creating channel content nodes...')
        
        
        course_list = json.load(open(os.path.join(COURSES_DIR, 'course_list.json')))
        for course in course_list['courses']: # [1:2]:
            basedir = os.path.join(COURSES_DIR, course['name'])
            coursedir = os.path.join(basedir, 'course')
            course_data = extract_course_tree(coursedir)
            for k, v in course_data.items():
                if k in ['children', 'certificates', 'pdf_textbooks']:
                    continue
                print(k,'=', v)
            # print_course(course_data, translate_from='ar')
            clean_subtree(course_data)
            print('\n\n\n')


    def pre_run(self, args, options):
        """
        Build the ricecooker json tree for the entire channel.
        """
        LOGGER.info('in pre_run...')

        ricecooker_json_tree = dict(
            title='Edraak (العربيّة)',          # a humand-readbale title
            source_domain=EDRAAK_COURSES_DOMAIN,       # content provider's domain
            source_id='courses',         # an alphanumeric channel ID
            description=EDRAAK_COURSES_CHANNEL_DESCRIPTION,
            thumbnail='./chefdata/edraak-logo.png',   # logo created from SVG
            language=getlang('ar').code    ,          # language code of channel
            children=[],
        )
        self.add_content_nodes(ricecooker_json_tree)
        # self.add_sample_content_nodes(ricecooker_json_tree)

        json_tree_path = self.get_json_tree_path()
        write_tree_to_json_tree(json_tree_path, ricecooker_json_tree)


    def run(self, args, options):
        print('in run')
        self.pre_run(args, options)
        print('DONE')



# CLI
################################################################################

if __name__ == '__main__':
    """
    This code will run when the sushi chef script is called on the command line.
    """
    chef = EdraakCoursesChef()
    chef.main()