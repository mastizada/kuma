from __future__ import with_statement
import os
import logging
from datetime import datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sitemaps import GenericSitemap
from django.core.cache import get_cache
from django.core.mail import EmailMessage, send_mail
from django.db import connection, transaction
from django.dispatch import receiver
from django.template.loader import render_to_string
from django.utils.encoding import smart_str

from celery.task import task
from constance import config
from xml.dom.minidom import parseString

from kuma.core.utils import MemcacheLock

from .events import context_dict
from .exceptions import PageMoveError, StaleDocumentsRenderingInProgress
from .helpers import absolutify
from .models import Document, Revision, RevisionIP
from .signals import render_done


log = logging.getLogger('kuma.wiki.tasks')


@task(rate_limit='60/m')
def render_document(pk, cache_control, base_url):
    """Simple task wrapper for the render() method of the Document model"""
    document = Document.objects.get(pk=pk)
    document.render(cache_control, base_url)
    return document.rendered_errors


@task(throws=(StaleDocumentsRenderingInProgress,))
def render_stale_documents(log=None):
    """Simple task wrapper for rendering stale documents"""
    lock = MemcacheLock('render-stale-documents-lock', expires=60 * 60)
    if lock.acquired:
        # fail loudly if this is running already
        # may indicate a problem with the schedule of this task
        raise StaleDocumentsRenderingInProgress

    stale_docs = Document.objects.get_by_stale_rendering()
    stale_docs_count = stale_docs.count()
    if stale_docs_count == 0:
        # not stale documents to render
        return

    if log is None:
        # fetch a logger in case none is given
        log = render_stale_documents.get_logger()

    log.info("Found %s stale documents" % stale_docs_count)
    response = None
    if lock.acquire():
        try:
            for doc in stale_docs:
                doc.render('no-cache', settings.SITE_URL)
                log.info("Rendered stale %s" % doc)
        finally:
            lock.release()
    return response


@task
def build_json_data_for_document_task(pk, stale):
    """Force-refresh cached JSON data after rendering."""
    document = Document.objects.get(pk=pk)
    document.get_json_data(stale=stale)


@receiver(render_done)
def build_json_data_handler(sender, instance, **kwargs):
    try:
        build_json_data_for_document_task.delay(instance.pk, stale=False)
    except:
        logging.error('JSON metadata build task failed',
                      exc_info=True)


@task
def move_page(locale, slug, new_slug, email):
    with transaction.commit_manually():
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            transaction.rollback()
            logging.error('Page move failed: no user with email address %s' %
                          email)
            return
        try:
            doc = Document.objects.get(locale=locale, slug=slug)
        except Document.DoesNotExist:
            transaction.rollback()
            message = """
    Page move failed.

    Move was requested for document with slug %(slug)s in locale
    %(locale)s, but no such document exists.
            """ % {'slug': slug, 'locale': locale}
            logging.error(message)
            send_mail('Page move failed', message, settings.DEFAULT_FROM_EMAIL,
                      [user.email])
            return
        try:
            doc._move_tree(new_slug, user=user)
        except PageMoveError as e:
            transaction.rollback()
            message = """
    Page move failed.

    Move was requested for document with slug %(slug)s in locale
    %(locale)s, but could not be completed.

    Diagnostic info:

    %(message)s
            """ % {'slug': slug, 'locale': locale, 'message': e.message}
            logging.error(message)
            send_mail('Page move failed', message, settings.DEFAULT_FROM_EMAIL,
                      [user.email])
            return
        except Exception as e:
            transaction.rollback()
            message = """
    Page move failed.

    Move was requested for document with slug %(slug)s in locale %(locale)s,
    but could not be completed.

    %(info)s
            """ % {'slug': slug, 'locale': locale, 'info': e}
            logging.error(message)
            send_mail('Page move failed', message, settings.DEFAULT_FROM_EMAIL,
                      [user.email])
            return

        transaction.commit()

    # Now that we know the move succeeded, re-render the whole tree.
    for moved_doc in [doc] + doc.get_descendants():
        moved_doc.schedule_rendering('max-age=0')

    subject = 'Page move completed: ' + slug + ' (' + locale + ')'
    full_url = settings.SITE_URL + '/' + locale + '/docs/' + new_slug
    message = """
Page move completed.

The move requested for the document with slug %(slug)s in locale
%(locale)s, and all its children, has been completed.

You can now view this document at its new location: %(full_url)s.
    """ % {'slug': slug, 'locale': locale, 'full_url': full_url}
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL,
              [user.email])


@task
def update_community_stats():
    cursor = connection.cursor()
    try:
        cursor.execute("""
            SELECT count(creator_id)
            FROM
              (SELECT DISTINCT creator_id
               FROM wiki_revision
               WHERE created >= DATE_SUB(NOW(), INTERVAL 1 YEAR)) AS contributors
            """)
        contributors = cursor.fetchone()

        cursor.execute("""
            SELECT count(locale)
            FROM
              (SELECT DISTINCT wd.locale
               FROM wiki_document wd,
                                  wiki_revision wr
               WHERE wd.id = wr.document_id
                 AND wr.created >= DATE_SUB(NOW(), INTERVAL 1 YEAR)) AS locales
            """)
        locales = cursor.fetchone()
    finally:
        cursor.close()

    community_stats = {}

    try:
        community_stats['contributors'] = contributors[0]
        community_stats['locales'] = locales[0]
    except IndexError:
        community_stats = None

    # storing a None value in cache allows a better check for
    # emptiness in the view
    if 0 in community_stats.values():
        community_stats = None

    cache = get_cache('memcache')
    cache.set('community_stats', community_stats, 86400)


@task
def delete_old_revision_ips(immediate=False, days=30):
    RevisionIP.objects.delete_old(days=days)


@task
def send_first_edit_email(revision_pk):
    """ Make an 'edited' notification email for first-time editors """
    revision = Revision.objects.get(pk=revision_pk)
    user, doc = revision.creator, revision.document
    subject = (u"[MDN] %(user)s made their first edit, to: %(doc)s" %
               {'user': user.username, 'doc': doc.title})
    message = render_to_string('wiki/email/edited.ltxt',
                               context_dict(revision))
    doc_url = absolutify(doc.get_absolute_url())
    email = EmailMessage(subject, message, settings.DEFAULT_FROM_EMAIL,
                         to=[config.EMAIL_LIST_FOR_FIRST_EDITS],
                         headers={'X-Kuma-Document-Url': doc_url,
                                  'X-Kuma-Editor-Username': user.username})
    email.send()


class WikiSitemap(GenericSitemap):
    protocol = 'https'
    priority = 0.5


@task
def build_sitemaps():
    sitemap_element = '<sitemap><loc>%s</loc><lastmod>%s</lastmod></sitemap>'
    sitemap_index = ('<sitemapindex xmlns="http://www.sitemaps.org/'
                     'schemas/sitemap/0.9">')
    now = datetime.utcnow()
    timestamp = '%s+00:00' % now.replace(microsecond=0).isoformat()
    for locale in settings.MDN_LANGUAGES:
        queryset = (Document.objects
                            .filter(is_template=False,
                                    locale=locale,
                                    is_redirect=False)
                            .exclude(title__startswith='User:')
                            .exclude(slug__icontains='Talk:'))
        if queryset.count() > 0:
            info = {'queryset': queryset, 'date_field': 'modified'}
            sitemap = WikiSitemap(info)
            urls = sitemap.get_urls(page=1)
            xml = smart_str(render_to_string('wiki/sitemap.xml',
                                             {'urlset': urls}))
            directory = os.path.join(settings.MEDIA_ROOT, 'sitemaps', locale)
            if not os.path.exists(directory):
                os.makedirs(directory)
            with open(os.path.join(directory, 'sitemap.xml'), 'w') as f:
                f.write(xml)

            sitemap_url = absolutify('/sitemaps/%s/sitemap.xml' % locale)
            sitemap_index = (sitemap_index + sitemap_element %
                             (sitemap_url, timestamp))

    sitemap_index = sitemap_index + "</sitemapindex>"

    index_path = os.path.join(settings.MEDIA_ROOT, 'sitemap.xml')
    with open(index_path, 'w') as index_file:
        index_file.write(parseString(sitemap_index).toxml())
