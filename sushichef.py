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

ORIGINAL_TREES_DIR = os.path.join('chefdata', 'originaltrees')
CLEAN_TREES_DIR = os.path.join('chefdata', 'cleantrees')
TRANSFORMED_TREES_DIR = os.path.join('chefdata', 'transformedtrees')

COURSES_DIR = 'chefdata/Courses'
SKIP_KINDS = ['wiki']   # edX content kinds not suported in Kolibri
EDRAAK_STRINGS = {
    "learning_objectives": [
        'الأهداف التعليمية',
        'الاهداف التعليمية'
    ],
    "knowledge_check": [
        'التحقق من المعرفة',
    ],
    "course_plan": [
        'خطة المساق',       # could be a sequential with a PDF resouce or HTML
    ]
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
    # 'قضية للنقاش',                # Issue for discussion
    # 'إسأل الدكتور أحمد',          # Ask Dr. Ahmed
]

EDRAAK_DROP_ICONS = [
    '/static/lightbulb.jpg',
    '/static/bald22.jpg',
    '/static/mic.png',
    '/static/exam.jpg',
    #
    '/static/Week1-Progress.png',
    '/static/Week2-Progress.png',
    '/static/Week3-Progress.png',
    '/static/FirstAid-Instructor-Video.png',
    '/static/FirstAid-Instructor-question2.png',
    '/static/FirstAid-Instructor-discussion.png',
    '/static/rsz_swift_logo_rgb.jpg',
]





# CLEAN, PRUNE, AND PROCESS TREE
################################################################################

def guess_vertical_type(vertical):
    title = vertical.get('display_name')
    if title and any(lo in title for lo in EDRAAK_STRINGS['learning_objectives']):
        return 'learning_objectives_vertical'

    if title and any(kc in title for kc in EDRAAK_STRINGS['knowledge_check']):
        return 'knowledge_check_vertical'

    children_kinds = set([child['kind'] for child in vertical['children']])
    if 'discussion' in children_kinds:
        return 'discussion_vertical'
    elif 'video' in children_kinds:
        return 'video_vertical'
    elif 'problem' in children_kinds and 'video' not in children_kinds:
        return 'test_vertical'
    elif children_kinds == set(['html']):
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
        else:
            print('found image', img['src'])

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

    if translate_from and DEBUG_MODE:
        text_en = translate_to_en(text, source_language=translate_from)
        text = text_en + ' ' + text

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








# PARSE PROBLEMS --> EXERCISE QUESTIONS (leaving node in place)
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


# TRANSFORM
################################################################################

def transform_vertical_to_exercise(vertical, parent_title=None, istest=False):
    """
    Parse an Edraaak `test_vertical' or `knowledge_check_vertical` to exercise.
    """
    if 'children' not in vertical:
        return None

    description = ''
    # Extract an optional description from the first html node
    first_child = vertical['children'][0]
    if first_child['kind'] == 'html':
        description = extract_text_from_html_item(first_child, translate_from='ar')

    if parent_title:
        exercise_title = parent_title + ' ' + vertical['display_name']
    else:
        exercise_title = vertical['display_name']


    # Exercise node
    exercise_dict = dict(
        kind=content_kinds.EXERCISE,
        title=exercise_title,
        author='Edraak',
        source_id=vertical['url_name'],
        description=description,
        language=getlang('ar').code,
        license=EDRAAK_LICENSE,
        exercise_data={
            'mastery_model': exercises.M_OF_N,
            'randomize': False,
            'm': 5,                   # By default require 3 to count as mastery
        },
        # thumbnail=
        questions=[],
    )

    for child in vertical['children']:
        if child['kind'] == 'problem':
            parsed_problem = parse_questions_from_problem(child)
            exercise_dict['questions'].extend(parsed_problem['questions'])

    # Update m in case less than 3 quesitons in the exercise
    if len(exercise_dict['questions']) < 5:
        exercise_dict['exercise_data']['m'] = len(exercise_dict['questions'])

    return exercise_dict




def transform_tree(clean_tree, coursedir):
    course_id = clean_tree['course']
    course_title = clean_tree['display_name']
    course_thumbnail = os.path.join(coursedir, 'static', clean_tree['course_image'])
    course_dict = dict(
        kind=content_kinds.TOPIC,
        title=course_title,
        source_id=course_id,
        description='',
        language=getlang('ar').code,
        license=EDRAAK_LICENSE,
        children=[],
    )

    for chapter in clean_tree['children']:
        chapter_dict = dict(
            kind=content_kinds.TOPIC,
            title=chapter['display_name'],
            source_id=chapter['url_name'],
            description='',
            language=getlang('ar').code,
            license=EDRAAK_LICENSE,
            children=[],
        )
        course_dict['children'].append(chapter_dict)

        for sequential in chapter['children']:

            children_vertical_types = set([guess_vertical_type(v) for v in sequential['children']])

            if len(sequential['children']) == 0:
                # containers of discussions end up as empty `sequential` nodes
                print('Skipping empty sequential', sequential)

            elif all(cvt == 'test_vertical' for cvt in children_vertical_types):
                # Hoist exam questions up to chapter level
                verticals = sequential['children']
                assert len(verticals) == 1, 'too many verticals found in test sequential ' + str(sequential)
                vertical = verticals[0]
                exercise_dict = transform_vertical_to_exercise(vertical)
                chapter_dict['children'].append(exercise_dict)

            else:
                # Process as regular folder
                sequential_dict = dict(
                    kind=content_kinds.TOPIC,
                    title=sequential['display_name'],
                    source_id=sequential['url_name'],
                    description=sequential.get('description', ''),
                    language=getlang('ar').code,
                    license=EDRAAK_LICENSE,
                    children=[],
                )
                chapter_dict['children'].append(sequential_dict)

                for vertical in sequential['children']:
                    vertical_type = guess_vertical_type(vertical)
                    if vertical_type in ['knowledge_check_vertical', 'test_vertical']:
                        exercise_dict = transform_vertical_to_exercise(vertical)
                        sequential_dict['children'].append(exercise_dict)

                    else:
                        print('skipping', vertical_type, vertical['url_name'])

    return course_dict


# DEBUG TREE PRINTING
################################################################################

def print_transfomed_tree(transfomed_tree, translate_from=None):
    """
    Display transformed course tree for debugging purposes.
    """
    EXTRA_FIELDS = ['description', 'youtube_id', 'path', 'text']

    def print_transfomed_subtree(subtree, indent=0):
        title = subtree['title']
        if translate_from:
            title_en = translate_to_en(title, source_language=translate_from)
            title = title_en  + ' ' + title
        extra = ''
        print('   '*indent, '-', title,  'kind='+subtree['kind'], '\t', extra)
        if 'children' in subtree:
            for child in subtree['children']:
                print_transfomed_subtree(child, indent=indent+1)
    print_transfomed_subtree(transfomed_tree)
    print('\n')



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
            course_id = course_data['course']
            write_tree_to_json_tree(os.path.join(ORIGINAL_TREES_DIR, course_id+'.json'), course_data)
            # print_course(course_data, translate_from='ar')
            clean_subtree(course_data, coursedir)
            write_tree_to_json_tree(os.path.join(CLEAN_TREES_DIR, course_id+'.json'), course_data)
            transformed_tree = transform_tree(course_data, coursedir)
            write_tree_to_json_tree(os.path.join(TRANSFORMED_TREES_DIR, course_id+'.json'), transformed_tree)
            print_transfomed_tree(transformed_tree, translate_from='ar')
            channel['children'].append(transformed_tree)
            print('\n\n')


    def pre_run(self, args, options):
        """
        Build the ricecooker json tree for the entire channel.
        """
        LOGGER.info('in pre_run...')

        ricecooker_json_tree = dict(
            title='Edraak Courses (العربيّة)',          # a humand-readbale title
            source_domain=EDRAAK_COURSES_DOMAIN,       # content provider's domain
            source_id='continuing-education-courses',  # an alphanumeric channel ID
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

