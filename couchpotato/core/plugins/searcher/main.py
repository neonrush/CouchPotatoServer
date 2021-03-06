from couchpotato import get_session
from couchpotato.core.event import addEvent, fireEvent
from couchpotato.core.helpers.encoding import simplifyString, toUnicode
from couchpotato.core.helpers.variable import md5, getImdb
from couchpotato.core.logger import CPLog
from couchpotato.core.plugins.base import Plugin
from couchpotato.core.settings.model import Movie, Release, ReleaseInfo
from couchpotato.environment import Env
from sqlalchemy.exc import InterfaceError
import datetime
import re
import time
import traceback

log = CPLog(__name__)


class Searcher(Plugin):

    in_progress = False

    def __init__(self):
        addEvent('searcher.all', self.all_movies)
        addEvent('searcher.single', self.single)
        addEvent('searcher.correct_movie', self.correctMovie)
        addEvent('searcher.download', self.download)

        # Schedule cronjob
        fireEvent('schedule.cron', 'searcher.all', self.all_movies, day = self.conf('cron_day'), hour = self.conf('cron_hour'), minute = self.conf('cron_minute'))

    def all_movies(self):

        if self.in_progress:
            log.info('Search already in progress')
            return

        self.in_progress = True

        db = get_session()

        movies = db.query(Movie).filter(
            Movie.status.has(identifier = 'active')
        ).all()

        for movie in movies:
            movie_dict = movie.to_dict({
                'profile': {'types': {'quality': {}}},
                'releases': {'status': {}, 'quality': {}},
                'library': {'titles': {}, 'files':{}},
                'files': {}
            })

            try:
                self.single(movie_dict)
            except IndexError:
                fireEvent('library.update', movie_dict['library']['identifier'], force = True)
            except:
                log.error('Search failed for %s: %s' % (movie_dict['library']['identifier'], traceback.format_exc()))

            # Break if CP wants to shut down
            if self.shuttingDown():
                break

        self.in_progress = False

    def single(self, movie):

        done_status = fireEvent('status.get', 'done', single = True)

        if not movie['profile'] or movie['status_id'] == done_status.get('id'):
            log.debug('Movie doesn\'t have a profile or already done, assuming in manage tab.')
            return

        db = get_session()

        pre_releases = fireEvent('quality.pre_releases', single = True)
        release_dates = fireEvent('library.update_release_date', identifier = movie['library']['identifier'], merge = True)
        available_status = fireEvent('status.get', 'available', single = True)

        default_title = movie['library']['titles'][0]['title']
        for quality_type in movie['profile']['types']:
            if not self.couldBeReleased(quality_type['quality']['identifier'], release_dates, pre_releases):
                log.info('To early to search for %s, %s' % (quality_type['quality']['identifier'], default_title))
                continue

            has_better_quality = 0

            # See if beter quality is available
            for release in movie['releases']:
                if release['quality']['order'] <= quality_type['quality']['order'] and release['status_id'] is not available_status.get('id'):
                    has_better_quality += 1

            # Don't search for quality lower then already available.
            if has_better_quality is 0:

                log.info('Search for %s in %s' % (default_title, quality_type['quality']['label']))
                quality = fireEvent('quality.single', identifier = quality_type['quality']['identifier'], single = True)
                results = fireEvent('yarr.search', movie, quality, merge = True)
                sorted_results = sorted(results, key = lambda k: k['score'], reverse = True)
                if len(sorted_results) == 0:
                    log.debug('Nothing found for %s in %s' % (default_title, quality_type['quality']['label']))

                # Add them to this movie releases list
                for nzb in sorted_results:

                    rls = db.query(Release).filter_by(identifier = md5(nzb['url'])).first()
                    if not rls:
                        rls = Release(
                            identifier = md5(nzb['url']),
                            movie_id = movie.get('id'),
                            quality_id = quality_type.get('quality_id'),
                            status_id = available_status.get('id')
                        )
                        db.add(rls)
                        db.commit()
                    else:
                        [db.delete(info) for info in rls.info]
                        db.commit()

                    for info in nzb:
                        try:
                            if not isinstance(nzb[info], (str, unicode, int, long)):
                                continue

                            rls_info = ReleaseInfo(
                                identifier = info,
                                value = toUnicode(nzb[info])
                            )
                            rls.info.append(rls_info)
                            db.commit()
                        except InterfaceError:
                            log.debug('Couldn\'t add %s to ReleaseInfo: %s' % (info, traceback.format_exc()))


                for nzb in sorted_results:
                    downloaded = self.download(data = nzb, movie = movie)
                    if downloaded:
                        return True
                    else:
                        break
            else:
                log.info('Better quality (%s) already available or snatched for %s' % (quality_type['quality']['label'], default_title))
                fireEvent('movie.restatus', movie['id'])
                break

            # Break if CP wants to shut down
            if self.shuttingDown():
                break

        db.remove()
        return False

    def download(self, data, movie, manual = False):

        snatched_status = fireEvent('status.get', 'snatched', single = True)
        successful = fireEvent('download', data = data, movie = movie, manual = manual, single = True)

        if successful:

            # Mark release as snatched
            db = get_session()
            rls = db.query(Release).filter_by(identifier = md5(data['url'])).first()
            rls.status_id = snatched_status.get('id')
            db.commit()

            log_movie = '%s (%s) in %s' % (movie['library']['titles'][0]['title'], movie['library']['year'], rls.quality.label)
            snatch_message = 'Snatched "%s": %s' % (data.get('name'), log_movie)
            log.info(snatch_message)
            fireEvent('movie.snatched', message = snatch_message, data = rls.to_dict())


            # If renamer isn't used, mark movie done
            if not Env.setting('enabled', 'renamer'):
                active_status = fireEvent('status.get', 'active', single = True)
                done_status = fireEvent('status.get', 'done', single = True)
                try:
                    if movie['status_id'] == active_status.get('id'):
                        for profile_type in movie['profile']['types']:
                            if profile_type['quality_id'] == rls.quality.id and profile_type['finish']:
                                log.info('Renamer disabled, marking movie as finished: %s' % log_movie)

                                # Mark release done
                                rls.status_id = done_status.get('id')
                                db.commit()

                                # Mark movie done
                                mvie = db.query(Movie).filter_by(id = movie['id']).first()
                                mvie.status_id = done_status.get('id')
                                db.commit()
                except Exception, e:
                    log.error('Failed marking movie finished: %s %s' % (e, traceback.format_exc()))

            return True

        log.info('Tried to download, but none of the downloaders are enabled')
        return False

    def correctMovie(self, nzb = {}, movie = {}, quality = {}, **kwargs):

        imdb_results = kwargs.get('imdb_results', False)
        single_category = kwargs.get('single_category', False)
        retention = Env.setting('retention', section = 'nzb')

        if nzb.get('seeds') is None and retention < nzb.get('age', 0):
            log.info('Wrong: Outside retention, age is %s, needs %s or lower: %s' % (nzb['age'], retention, nzb['name']))
            return False

        movie_name = simplifyString(nzb['name'])
        nzb_words = re.split('\W+', movie_name)
        required_words = [x.strip() for x in self.conf('required_words').split(',')]

        if self.conf('required_words') and not list(set(nzb_words) & set(required_words)):
            log.info("NZB doesn't contain any of the required words.")
            return False

        ignored_words = [x.strip() for x in self.conf('ignored_words').split(',')]
        blacklisted = list(set(nzb_words) & set(ignored_words))
        if self.conf('ignored_words') and blacklisted:
            log.info("Wrong: '%s' blacklisted words: %s" % (nzb['name'], ", ".join(blacklisted)))
            return False

        pron_tags = ['xxx', 'sex', 'anal', 'tits', 'fuck', 'porn', 'orgy', 'milf', 'boobs']
        for p_tag in pron_tags:
            if p_tag in movie_name:
                log.info('Wrong: %s, probably pr0n' % (nzb['name']))
                return False

        #qualities = fireEvent('quality.all', single = True)
        preferred_quality = fireEvent('quality.single', identifier = quality['identifier'], single = True)

        # Contains lower quality string
        if self.containsOtherQuality(nzb, movie_year = movie['library']['year'], preferred_quality = preferred_quality, single_category = single_category):
            log.info('Wrong: %s, looking for %s' % (nzb['name'], quality['label']))
            return False


        # File to small
        if nzb['size'] and preferred_quality['size_min'] > nzb['size']:
            log.info('"%s" is too small to be %s. %sMB instead of the minimal of %sMB.' % (nzb['name'], preferred_quality['label'], nzb['size'], preferred_quality['size_min']))
            return False

        # File to large
        if nzb['size'] and preferred_quality.get('size_max') < nzb['size']:
            log.info('"%s" is too large to be %s. %sMB instead of the maximum of %sMB.' % (nzb['name'], preferred_quality['label'], nzb['size'], preferred_quality['size_max']))
            return False


        if imdb_results:
            return True

        # Check if nzb contains imdb link
        if self.checkIMDB([nzb['description']], movie['library']['identifier']):
            return True

        for movie_title in movie['library']['titles']:
            movie_words = re.split('\W+', simplifyString(movie_title['title']))

            if self.correctName(nzb['name'], movie_title['title']):
                # if no IMDB link, at least check year range 1
                if len(movie_words) > 2 and self.correctYear([nzb['name']], movie['library']['year'], 1):
                    return True

                # if no IMDB link, at least check year
                if len(movie_words) <= 2 and self.correctYear([nzb['name']], movie['library']['year'], 0):
                    return True

        # Get the nfo and see if it contains the proper imdb url
        if self.checkNFO(nzb['name'], movie['library']['identifier']):
            return True

        log.info("Wrong: %s, undetermined naming. Looking for '%s (%s)'" % (nzb['name'], movie['library']['titles'][0]['title'], movie['library']['year']))
        return False

    def containsOtherQuality(self, nzb, movie_year = None, preferred_quality = {}, single_category = False):

        name = nzb['name']
        size = nzb.get('size', 0)
        nzb_words = re.split('\W+', simplifyString(name))

        qualities = fireEvent('quality.all', single = True)

        found = {}
        for quality in qualities:
            # Main in words
            if quality['identifier'] in nzb_words:
                found[quality['identifier']] = True

            # Alt in words
            if list(set(nzb_words) & set(quality['alternative'])):
                found[quality['identifier']] = True

        # Hack for older movies that don't contain quality tag
        year_name = fireEvent('scanner.name_year', name, single = True)
        if movie_year < datetime.datetime.now().year - 3 and not year_name.get('year', None):
            if size > 3000: # Assume dvdr
                return 'dvdr' == preferred_quality['identifier']
            else: # Assume dvdrip
                return 'dvdrip' == preferred_quality['identifier']

        # Allow other qualities
        for allowed in preferred_quality.get('allow'):
            if found.get(allowed):
                del found[allowed]

        if (len(found) == 0 and single_category):
            return False

        return not (found.get(preferred_quality['identifier']) and len(found) == 1)

    def checkIMDB(self, haystack, imdbId):

        for string in haystack:
            if 'imdb.com/title/' + imdbId in string:
                return True

        return False

    def correctYear(self, haystack, year, range):

        for string in haystack:
            if str(year) in string or str(int(year) + range) in string or str(int(year) - range) in string: # 1 year of is fine too
                return True

        return False

    def correctName(self, check_name, movie_name):

        check_names = [check_name]
        try:
            check_names.append(re.search(r'([\'"])[^\1]*\1', check_name).group(0))
        except:
            pass

        for check_name in check_names:
            check_movie = fireEvent('scanner.name_year', check_name, single = True)

            try:
                check_words = filter(None, re.split('\W+', check_movie.get('name', '')))
                movie_words = filter(None, re.split('\W+', simplifyString(movie_name)))

                if len(check_words) > 0 and len(movie_words) > 0 and len(list(set(check_words) - set(movie_words))) == 0:
                    return True
            except:
                pass

        return False

    def checkNFO(self, check_name, imdb_id):
        cache_key = 'srrdb.com %s' % simplifyString(check_name)

        nfo = self.getCache(cache_key)
        if not nfo:
            try:
                nfo = self.urlopen('http://www.srrdb.com/showfile.php?release=%s' % check_name, show_error = False)
                self.setCache(cache_key, nfo)
            except:
                pass

        return nfo and getImdb(nfo) == imdb_id

    def couldBeReleased(self, wanted_quality, dates, pre_releases):

        now = int(time.time())

        if not dates or (dates.get('theater', 0) == 0 and dates.get('dvd', 0) == 0):
            return True
        else:
            if wanted_quality in pre_releases:
                # Prerelease 1 week before theaters
                if dates.get('theater') - 604800 < now:
                    return True
            else:
                # 6 weeks after theater release
                if dates.get('theater') + 3628800 < now:
                    return True

                # 6 weeks before dvd release
                if dates.get('dvd') - 3628800 < now:
                    return True

                # Dvd should be released
                if dates.get('dvd') > 0 and dates.get('dvd') < now:
                    return True


        return False
