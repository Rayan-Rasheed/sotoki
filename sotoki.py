#!/usr/bin/env python2
"""sotoki.

Usage:
  sotoki.py load <dump-directory> <database-directory>
  sotoki.py build <templates> <database> <output>
  sotoki.py (-h | --help)
  sotoki.py --version

Options:
  -h --help     Show this screen.
  --version     Show version.

"""
import os
import re
from string import punctuation

from json import dumps
from json import loads

from contextlib import contextmanager

from docopt import docopt

from markdown import markdown as md

from jinja2 import Environment
from jinja2 import FileSystemLoader

from lxml.etree import parse

from wiredtiger.packing import pack
from wiredtiger.packing import unpack
from wiredtiger import wiredtiger_open


def intspace(value):
    orig = str(value)
    new = re.sub("^(-?\d+)(\d{3})", '\g<1> \g<2>', orig)
    if orig == new:
        return new
    else:
        return intspace(new)


def markdown(text):
    # FIXME: add postprocess step to transform 'http://' into a link
    # strip p tags
    return md(text)[3:-4]


def scale(number):
    """Convert number to scale to be used in style to color arrows
    and comment score"""
    if number < 0:
        return 'negative'
    if number == 0:
        return 'zero'
    if number < 3:
        return 'positive'
    if number < 8:
        return 'good'
    return 'verygood'


def render(output, template, templates, **context):
    templates = os.path.abspath(templates)
    env = Environment(loader=FileSystemLoader((templates,)))
    filters = dict(
        markdown=markdown,
        intspace=intspace,
        scale=scale,
        clean=lambda y: filter(lambda x: x not in punctuation, y)
    )
    env.filters.update(filters)
    template = env.get_template(template)
    page = template.render(**context)
    with open(output, 'w') as f:
        f.write(page.encode('utf-8'))


# wiredtiger helper

WT_NOTFOUND = -31803


class TupleSpace(object):
    """Generic database"""

    def __init__(self, path):
        self.connection = wiredtiger_open(path, 'create')
        self.session = self.connection.open_session()
        self.session.create(
            'table:tuples',
            'key_format=SS,value_format=Qu,columns=(uid,name,kind,value)'
        )
        self.session.create(
            'index:tuples:index',
            'columns=(name,kind,value,uid)'
        )
        self.tuples_cursors = list()
        self.index_cursors = list()
        self.count = 0

    @contextmanager
    def tuples(self):
        if self.tuples_cursors:
            cursor = self.tuples_cursors.pop()
        else:
            cursor = self.session.open_cursor('table:tuples')
        try:
            yield cursor
        finally:
            cursor.reset()
            self.tuples_cursors.append(cursor)

    @contextmanager
    def index(self):
        if self.index_cursors:
            cursor = self.index_cursors.pop()
        else:
            cursor = self.session.open_cursor('index:tuples:index')
        try:
            yield cursor
        finally:
            cursor.reset()
            self.index_cursors.append(cursor)

    def _unpack_value(self, kind, value):
        if kind == 1:
            return unpack('Q', value)[0]
        elif kind == 2:
            return unpack('S', value)[0]
        else:
            return loads(unpack('S', value)[0])

    def _pack_value(self, value):
        # pack and set value
        if type(value) is int:
            return 1, pack('Q', value)
        elif type(value) is str:
            return 2, pack('S', value)
        else:
            return 3, pack('S', dumps(value))

    def insert(self, uid, name, value):
        # set key
        with self.tuples() as cursor:
            cursor.set_key(uid, name)
            value = self._pack_value(value)
            cursor.set_value(*value)
            cursor.insert()

    def get(self, uid):
        def __get():
            with self.tuples() as cursor:
                cursor.set_key(uid, '')
                ok = cursor.search_near()
                if ok == WT_NOTFOUND:
                    raise Exception('nothing found')
                if ok == -1:
                    cursor.next()
                while True:
                    other = cursor.get_key()
                    ok = reduce(
                        lambda x, y: (cmp(*y) == 0) and y,
                        zip((uid,), other),
                        True
                    )
                    if ok:
                        _, key = other
                        # XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
                        # XXX: remove namespace!!!
                        # XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
                        key = key.split('/')[1]
                        value = cursor.get_value()
                        yield key, self._unpack_value(*value)

                        if cursor.next() == WT_NOTFOUND:
                            break
                        else:
                            continue
                    else:
                        break
        return dict(__get())

    def query(self, name, value, uid):
        """Iterate a cursor over records matching `key`
        starting near `key`. You *must* finish the iteration
        before reusing `cursor`"""

        with self.index() as cursor:
            kind, value = self._pack_value(value)
            key = name, kind, value, uid
            cursor.set_key(*key)
            match = [e for e in key if e]
            ok = cursor.search_near()
            if ok == WT_NOTFOUND:
                raise Exception('nothing found')
            if ok == -1:
                cursor.next()
            while True:
                other = cursor.get_key()
                ok = reduce(
                    lambda x, y: (cmp(*y) == 0) and y,
                    zip(match, other),
                    True
                )
                if ok:
                    yield other, cursor.get_value()
                    if cursor.next() == WT_NOTFOUND:
                        break
                    else:
                        continue
                else:
                    break

    def close(self):
        self.session.close()
        self.connection.close()


# load

KEYS_TO_COERCE_TO_INT = [
    'Score',
    'FavoriteCount',
]


def load(dump, database):
    # init database
    os.makedirs(database)
    db = TupleSpace(database)

    # parse Posts
    def to_db(filename):
        name = filename.split('.')[0][:-1]
        print 'loading :', name
        xml = parse(os.path.join(dump, filename))
        posts = xml.getroot()
        for post in posts.iterchildren():
            # make sure this is a unique identifier
            uid = '%s:%s' % (name, post.attrib['Id'])
            # populate post attributes
            for key in post.keys():
                value = post.attrib[key]
                if key in KEYS_TO_COERCE_TO_INT:
                    if value:
                        value = int(value)
                # XXX: namespace keys with slash `/`
                key = '%s/%s' % (name, key)
                db.insert(uid, key, value)

    to_db('Posts.xml')
    to_db('Comments.xml')
    to_db('PostLinks.xml')
    # tag dump doesn't provide useful information
    # to_db('Tags.xml')
    to_db('Users.xml')

    # generate tag informations
    print 'load tag links'
    for question in get_questions(db):
        for tag in question['Tags']:
            # add tag to the global list of tags anyway
            db.insert('Tag:%s' % tag, 'name', tag)
            # create TagLink
            uid = 'TagLink/%s:%s' % (tag, question['Id'])
            db.insert(uid, tag, question['Id'])

    db.close()


# queries


def user(db, id):
    record = next(db.query('User/Id', id, ''))
    key, _ = record
    name, kind, value, uid = key
    return db.get(uid)


def get_post(db, id):
    post = db.get(id)
    # It's possible that there is no owner
    try:
        post['Author'] = user(db, post['OwnerUserId'])
    except:
        post['Author'] = None
    # sanitize tags if any
    try:
        post['Tags'] = post['Tags'][1:-1].split('><')
    except:
        pass

    return post


def related(db, id):
    # get related questions
    def __iter():
        records = db.query('PostLink/PostId', id, '')
        for key, _ in records:
            name, kind, value, uid = key
            link = db.get(uid)
            uid = 'Post:' + link['RelatedPostId']
            related = get_post(db, uid)
            related['Kind'] = link['LinkTypeId']
            yield related
    # XXX: not sure why there is no score
    out = sorted(list(__iter()), key=lambda x: x.get('Score', 0), reverse=True)
    return out


def comments(db, id):
    def __iter():
        records = db.query('Comment/PostId', id, '')
        for key, _ in records:
            name, kind, value, uid = key
            comment = db.get(uid)
            try:
                comment['Author'] = user(db, comment['UserId'])
            except KeyError:
                pass
            else:
                yield comment
    return sorted(list(__iter()), key=lambda x: x['CreationDate'])


def get_questions(db):
    for item in db.query('Post/PostTypeId', '1', ''):
        key, _ = item
        name, kind, value, uid = key
        yield get_post(db, uid)


def answers(db, id):
    # retrieve from `db` all tuples that have a key `ParentID`
    # and a value that is a string ie. kind `2` and value `id`
    def __iter():
        records = db.query('Post/ParentID', id, '')
        for key, _ in records:
            name, kind, value, uid = key
            answer = get_post(db, uid)
            yield answer, comments(db, answer['Id'])
    return sorted(list(__iter()), key=lambda x: x[0]['Score'], reverse=True)


# build


def build(templates, database, output):
    db = TupleSpace(database)
    os.makedirs(os.path.join(output, 'posts'))
    questions = get_questions(db)
    for num, question in enumerate(questions):
        qs.append(question)  # debug
        print 'render post', num
        question_id = question['Id']
        render(
            os.path.join(output, 'posts', '%s.html' % question_id),
            'post.html',
            templates,
            post=question,
            related=related(db, question_id),
            comments=comments(db, question_id),
            answers=answers(db, question_id)
        )
        if num == 100:
            break
    print 'render', 'index.html'
    render(
        os.path.join(output, 'index.html'),
        'index.html',
        templates
    )
    # print 'render', 'search.js'
    # render(
    #     os.path.join(output, 'search.js'),
    #     'search.js',
    #     templates,
    #     questions=qs,  # debug
    # )
    print 'done'


if __name__ == '__main__':
    arguments = docopt(__doc__, version='sotoki 0.1')
    if arguments['load']:
        load(arguments['<dump-directory>'], arguments['<database-directory>'])
    elif arguments['build']:
        build(arguments['<templates>'], arguments['<database>'], arguments['<output>'])
