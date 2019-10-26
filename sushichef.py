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
from libedx import translate_to_en

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
    'إستبيان إنهاء المساق',       # Course Completion Questionnaire
    'استبيان نهاية المساق',       # End of course questionnaire
    'مفاجأة المساق',              # Surprise Course (contains only discussion vertical)
]

EDRAAK_DROP_ICONS = [
    '/static/lightbulb.jpg',
    '/static/bald22.jpg',
    '/static/mic.png',
    '/static/exam.jpg',
]





# CLEAN, PRUNE, AND PROCESS TREE
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


def clean_subtree(subtree, coursedir):
    kind = subtree['kind']
    title = subtree['display_name'] if 'display_name' in subtree else ''
    # print(kind, title, subtree.get('url_name', ''))
    if kind == 'video':
        subtree = process_video(subtree)
    elif kind == 'html':
        # check if downloadable resources HTML div first
        resources = extract_downloadable_resouces_from_html_item(subtree, coursedir=coursedir)
        subtree['downloadable_resources'] = resources
        if not resources:
            text = extract_text_from_html_item(subtree, translate_from='ar')
            subtree['text'] = text
    elif kind == 'problem':
        parse_questions_from_problem(subtree)


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

                elif vertical_type == 'discussion_vertical':
                    continue

                elif vertical_type == 'learning_objectives_vertical':
                    htmlgrandchildren = child['children']
                    htmlgrandchild = htmlgrandchildren[0]
                    text = extract_text_from_html_item(htmlgrandchild, translate_from='ar')
                    subtree['description'] = text
                    if len(htmlgrandchildren) > 1:
                        print('skipping', htmlgrandchildren[1:])
                    continue

            # Recurse
            clean_child = clean_subtree(child, coursedir=coursedir)
            new_children.append(clean_child)
    subtree['children'] = new_children

    return subtree





# TREE FUNCTIONS FOR PARSING EDRAAK COURSES
################################################################################


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



def extract_downloadable_resouces_from_html_item(item, coursedir):
    """
    Extracts the resource links from an edX HTML content item.
    Returns:
        [
            {
                'url': 'https://s3.amazonaws.com/hp-life-content/.../Hoja+de+trabajo.docx',
                'ext': 'docx',
                'filename': 'Hoja de trabajo.docx',
                'title': 'Hoja de trabajo',
                'link_html': '<a href={href} ...><othertags...>{title}</a>''
            },
            ...
        ]
    """
    resources = []
    assert item['kind'] == 'html'
    html = item['content']
    doc = BeautifulSoup(html, 'html5lib')

    # CASE A: PDF in iframe
    iframe = doc.find('iframe')
    if iframe:
        href = iframe['src'].strip()
        filename = os.path.basename(href)
        
        if href.startswith('/static'):
            relhref = coursedir + href
            if not os.path.exists(relhref):
                relhref = relhref.replace('_', ' ')
                if not os.path.exists(relhref):
                    print('file not fount at relhref', relhref)
        else:
            print('unknown href', href)
        _, dotext = os.path.splitext(filename)
        ext = dotext[1:].lower()
        resource = dict(
            relhref=relhref,
            ext=ext,
            filename=filename,
            title=iframe.get('title', 'no title'),
            link_html = str(iframe),
        )
        resources.append(resource)
        return resources

    # CASE B: Links to files
    links = doc.find_all('a')
    for link in links:
        href = link['href'].strip()
        filename = os.path.basename(href)

        if href.startswith('/static'):
            relhref = coursedir + href
            if not os.path.exists(relhref):
                relhref = relhref.replace('_', ' ')
                if not os.path.exists(relhref):
                    print('file not fount at relhref', relhref)
        else:
            print('unknown href', href)

        _, dotext = os.path.splitext(filename)
        ext = dotext[1:].lower()
        resource = dict(
            relhref=relhref,
            ext=ext,
            filename=filename,
            title=link.text.strip(),
            link_html = str(link),
        )
        resources.append(resource)
    return resources



def extract_text_from_html_item(item, translate_from=None):
    content = item['content']
    doc = BeautifulSoup(content, 'html5lib')
    body = doc.find('body')
    
    for img in body.find_all('img'):
        if img['src'] in EDRAAK_DROP_ICONS:
            img.decompose()

    page_text = html2text(str(body), bodywidth=0)
    page_text_lines = page_text.split('\n')
    non_blank_lines = [line for line in page_text_lines if line.strip()]
    
    # Clean and standardize line outputs
    clean_lines = []
    for line in non_blank_lines:
        line = line.replace('**', '').strip()
        line = line.replace('###', '').strip()
        line = line.replace('* · ', '•')
        line = line.replace('·', '•')
        line = line.replace('●', '•')
        line = line.replace('*', '•')
        if line.startswith('_'):
            line = line[1:]
        if line.endswith('_'):
            line = line[:-1]
        #
        if line:
            clean_lines.append(line)
    text = ' '.join(clean_lines)

    if translate_from:
        text_en = translate_to_en(text, source_language=translate_from)
        text = text_en # + ' ' + text

    return text




# def transform_html(content):
#     """
#     Transform the HTML markup taken from `content` (str) to file index.html in
#     a standalone zip file. Return the neceesary metadata as a dict.
#     """
#     chef_tmp_dir = 'chefdata/tmp'
#     webroot = tempfile.mkdtemp(dir=chef_tmp_dir)
# 
#     metadata = dict(
#         kind = 'html_content',
#         source_id = content[0:30],
#         zippath = None,  # to be set below
#     )
# 
#     doc = BeautifulSoup(content, 'html5lib')
#     meta = Tag(name='meta', attrs={'charset':'utf-8'})
#     doc.head.append(meta)
#     # TODO: add meta language (in case of right-to-left languages)
# 
#     # Writeout new index.html
#     indexhtmlpath = os.path.join(webroot, 'index.html')
#     with open(indexhtmlpath, 'w') as indexfilewrite:
#         indexfilewrite.write(str(doc))
# 
#     # Zip it
#     zippath = create_predictable_zip(webroot)
#     metadata['zippath'] = zippath
# 
#     return metadata








# PROBLEMS --> EXERCISE QUESTIONS
################################################################################

DROP_EXPLANATIONS = [
    'release of the iPod allowed consumers',
]


def parse_questions_from_problem(problem):
    assert problem['kind'] == 'problem'
    xml = problem['content']
    doc = BeautifulSoup(xml, "xml")
    problem_els = doc.find_all('problem')
    assert len(problem_els) == 1, 'found multiple problem elements'
    problem_el = problem_els[0]

    questions = []

    # A. SINGLE SELECT
    multiplechoiceresponses = problem_el.find_all('multiplechoiceresponse')
    for i, multiplechoiceresponse in enumerate(multiplechoiceresponses):
        
        question_p = multiplechoiceresponse.find_previous_sibling('p')
        question_text = question_p.text.strip()
        question_text = re.sub(' +', ' ', question_text)

        question_dict = dict(
            question_type=exercises.SINGLE_SELECTION,
            id=problem['url_name'] + '-' + str(i+1),
            question=question_text,
            correct_answer=None,
            all_answers=[],
            hints=[],
        )

        choicegroup = multiplechoiceresponse.find('choicegroup')
        choices = choicegroup.find_all('choice')
        for choice in choices:
            answer_text = choice.text.strip()
            question_dict['all_answers'].append(answer_text)
            if choice['correct'] == 'true':
                question_dict['correct_answer'] = answer_text

        # find solution element if it exists
        solution_el = multiplechoiceresponse.findNext('solution')
        if solution_el:
            solution_text = solution_el.text.strip()
            if not any(de in solution_text for de in DROP_EXPLANATIONS):
                question_dict['hints'].append(solution_text)
        
        questions.append(question_dict)


    # B. MULTIPLE SELECT
    choiceresponses  = problem_el.find_all('choiceresponse')
    for j, choiceresponse in enumerate(choiceresponses):

        question_p = choiceresponse.find_previous_sibling('p')
        question_text = question_p.text.strip()
        question_text = re.sub(' +', ' ', question_text)

        question_dict = dict(
            question_type=exercises.MULTIPLE_SELECTION,
            id=problem['url_name'] + '-' + str(j+1),
            question=question_text,
            correct_answers=[],
            all_answers=[],
            hints=[],
        )

        checkboxgroup = choiceresponse.find('checkboxgroup')
        choices = checkboxgroup.find_all('choice')
        for choice in choices:
            answer_text = choice.text.strip()
            question_dict['all_answers'].append(answer_text)
            if choice['correct'] == 'true':
                question_dict['correct_answers'].append(answer_text)

        # find solution element if it exists
        solution_el = choiceresponse.findNext('solution')
        if solution_el:
            solution_text = solution_el.text.strip()
            if not any(de in solution_text for de in DROP_EXPLANATIONS):
                question_dict['hints'].append(solution_text)

        questions.append(question_dict)

    if not questions:
        print(problem)
        raise ValueError('Parsing error -- no questoins found in this problem')

    problem['questions'] = questions
    return problem





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
            title='Edraak Courses (العربيّة)',          # a humand-readbale title
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


    # def run(self, args, options):
    #     print('in run')
    #     self.pre_run(args, options)
    #     print('DONE')



# CLI
################################################################################

if __name__ == '__main__':
    """
    This code will run when the sushi chef script is called on the command line.
    """
    chef = EdraakCoursesChef()
    chef.main()