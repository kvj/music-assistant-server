#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from typing import List
from utils import run_periodic, LOGGER, parse_track_title
from app_vars import get_app_var
from models import MusicProvider, MediaType, TrackQuality, AlbumType, Artist, Album, Track, Playlist
from constants import CONF_USERNAME, CONF_PASSWORD, CONF_ENABLED
import json
import aiohttp
import time
import datetime
import hashlib
from asyncio_throttle import Throttler
from modules.cache import use_cache


def setup(mass):
    ''' setup the provider'''
    enabled = mass.config["musicproviders"]['qobuz'].get(CONF_ENABLED)
    username = mass.config["musicproviders"]['qobuz'].get(CONF_USERNAME)
    password = mass.config["musicproviders"]['qobuz'].get(CONF_PASSWORD)
    if enabled and username and password:
        provider = QobuzProvider(mass, username, password)
        return provider
    return False

def config_entries():
    ''' get the config entries for this provider (list with key/value pairs)'''
    return [
        (CONF_ENABLED, False, CONF_ENABLED),
        (CONF_USERNAME, "", CONF_USERNAME), 
        (CONF_PASSWORD, "<password>", CONF_PASSWORD)
        ]

class QobuzProvider(MusicProvider):
    

    def __init__(self, mass, username, password):
        self.name = 'Qobuz'
        self.prov_id = 'qobuz'
        self.mass = mass
        self.cache = mass.cache
        self.http_session = aiohttp.ClientSession(loop=mass.event_loop, connector=aiohttp.TCPConnector(verify_ssl=False))
        self.__username = username
        self.__password = password
        self.__user_auth_info = None
        self.__logged_in = False
        self.throttler = Throttler(rate_limit=2, period=1)
        mass.add_event_listener(self.mass_event, 'streaming_started')
        mass.add_event_listener(self.mass_event, 'streaming_ended')

    async def search(self, searchstring, media_types=List[MediaType], limit=5):
        ''' perform search on the provider '''
        result = {
            "artists": [],
            "albums": [],
            "tracks": [],
            "playlists": []
        }
        params = {"query": searchstring, "limit": limit }
        if len(media_types) == 1:
            # qobuz does not support multiple searchtypes, falls back to all if no type given
            if media_types[0] == MediaType.Artist:
                params["type"] = "artists"
            if media_types[0] == MediaType.Album:
                params["type"] = "albums"
            if media_types[0] == MediaType.Track:
                params["type"] = "tracks"
            if media_types[0] == MediaType.Playlist:
                params["type"] = "playlists"
        searchresult = await self.__get_data("catalog/search", params)
        if searchresult:
            if "artists" in searchresult:
                for item in searchresult["artists"]["items"]:
                    artist = await self.__parse_artist(item)
                    if artist:
                        result["artists"].append(artist)
            if "albums" in searchresult:
                for item in searchresult["albums"]["items"]:
                    album = await self.__parse_album(item)
                    if album:
                        result["albums"].append(album)
            if "tracks" in searchresult:
                for item in searchresult["tracks"]["items"]:
                    track = await self.__parse_track(item)
                    if track:
                        result["tracks"].append(track)
            if "playlists" in searchresult:
                for item in searchresult["playlists"]["items"]:
                    result["playlists"].append(await self.__parse_playlist(item))
        return result
    
    async def get_library_artists(self) -> List[Artist]:
        ''' retrieve library artists from qobuz '''
        result = []
        params = {'type': 'artists'}
        for item in await self.__get_all_items("favorite/getUserFavorites", params, key='artists'):
            artist = await self.__parse_artist(item)
            if artist:
                result.append(artist)
        return result
    
    async def get_library_albums(self) -> List[Album]:
        ''' retrieve library albums from qobuz '''
        result = []
        params = {'type': 'albums'}
        for item in await self.__get_all_items("favorite/getUserFavorites", params, key='albums'):
            album = await self.__parse_album(item)
            if album:
                result.append(album)
        return result

    async def get_library_tracks(self) -> List[Track]:
        ''' retrieve library tracks from qobuz '''
        result = []
        params = {'type': 'tracks'}
        for item in await self.__get_all_items("favorite/getUserFavorites", params, key='tracks'):
            track = await self.__parse_track(item)
            if track:
                result.append(track)
        return result 

    async def get_playlists(self) -> List[Playlist]:
        ''' retrieve playlists from the provider '''
        result = []
        for item in await self.__get_all_items("playlist/getUserPlaylists", key='playlists', cache_checksum=time.time()):
            playlist = await self.__parse_playlist(item)
            if playlist:
                result.append(playlist)
        return result 

    async def get_artist(self, prov_artist_id) -> Artist:
        ''' get full artist details by id '''
        params = {'artist_id': prov_artist_id}
        artist_obj = await self.__get_data("artist/get", params)
        return await self.__parse_artist(artist_obj)

    async def get_album(self, prov_album_id) -> Album:
        ''' get full album details by id '''
        params = {'album_id': prov_album_id}
        album_obj = await self.__get_data("album/get", params)
        return await self.__parse_album(album_obj)

    async def get_track(self, prov_track_id) -> Track:
        ''' get full track details by id '''
        params = {'track_id': prov_track_id}
        track_obj = await self.__get_data("track/get", params)
        return await self.__parse_track(track_obj)

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        ''' get full playlist details by id '''
        params = {'playlist_id': prov_playlist_id}
        playlist_obj = await self.__get_data("playlist/get", params)
        return await self.__parse_playlist(playlist_obj)

    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        ''' get album tracks for given album id '''
        params = {'album_id': prov_album_id}
        track_objs = await self.__get_all_items("album/get", params, key='tracks')
        tracks = []
        for track_obj in track_objs:
            track = await self.__parse_track(track_obj)
            if track:
                tracks.append(track)
        return tracks

    async def get_playlist_tracks(self, prov_playlist_id, limit=100, offset=0) -> List[Track]:
        ''' get playlist tracks for given playlist id '''
        playlist_obj = await self.__get_data("playlist/get?playlist_id=%s" % prov_playlist_id, ignore_cache=True)
        cache_checksum = playlist_obj["updated_at"]
        params = {'playlist_id': prov_playlist_id, 'extra': 'tracks'}
        track_objs = await self.__get_all_items("playlist/get", params, key='tracks', limit=limit, offset=offset, cache_checksum=cache_checksum)
        tracks = []
        for track_obj in track_objs:
            playlist_track = await self.__parse_track(track_obj)
            if playlist_track:
                tracks.append(playlist_track)
            # TODO: should we look for an alternative track version if the original is marked unavailable ?
        return tracks

    async def get_artist_albums(self, prov_artist_id, limit=100, offset=0) -> List[Album]:
        ''' get a list of albums for the given artist '''
        params = {'artist_id': prov_artist_id, 'extra': 'albums', 'limit': limit, 'offset': offset}
        result = await self.__get_data('artist/get', params)
        albums = []
        for item in result['albums']['items']:
            if str(item['artist']['id']) == str(prov_artist_id):
                album = await self.__parse_album(item)
                if album:
                    albums.append(album)
        return albums

    async def get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        ''' get a list of most popular tracks for the given artist '''
        # artist toptracks not supported on Qobuz, so use search instead
        items = []
        artist = await self.get_artist(prov_artist_id)
        params = {"query": artist.name, "limit": 10, "type": "tracks" }
        searchresult = await self.__get_data("catalog/search", params)
        for item in searchresult["tracks"]["items"]:
            if "performer" in item and str(item["performer"]["id"]) == str(prov_artist_id):
                track = await self.__parse_track(item)
                items.append(track)
        return items
    
    async def add_library(self, prov_item_id, media_type:MediaType):
        ''' add item to library '''
        if media_type == MediaType.Artist:
            result = await self.__get_data('favorite/create', {'artist_ids': prov_item_id})
            item = await self.artist(prov_item_id)
        elif media_type == MediaType.Album:
            result = await self.__get_data('favorite/create', {'album_ids': prov_item_id})
            item = await self.album(prov_item_id)
        elif media_type == MediaType.Track:
            result = await self.__get_data('favorite/create', {'track_ids': prov_item_id})
            item = await self.track(prov_item_id)
        await self.mass.db.add_to_library(item.item_id, media_type, self.prov_id)
        LOGGER.debug("added item %s to %s - %s" %(prov_item_id, self.prov_id, result))

    async def remove_library(self, prov_item_id, media_type:MediaType):
        ''' remove item from library '''
        if media_type == MediaType.Artist:
            result = await self.__get_data('favorite/delete', {'artist_ids': prov_item_id})
            item = await self.artist(prov_item_id)
        elif media_type == MediaType.Album:
            result = await self.__get_data('favorite/delete', {'album_ids': prov_item_id})
            item = await self.album(prov_item_id)
        elif media_type == MediaType.Track:
            result = await self.__get_data('favorite/delete', {'track_ids': prov_item_id})
            item = await self.track(prov_item_id)
        await self.mass.db.remove_from_library(item.item_id, media_type, self.prov_id)
        LOGGER.debug("deleted item %s from %s - %s" %(prov_item_id, self.prov_id, result))
    
    async def add_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        ''' add track(s) to playlist '''
        params = {
            'playlist_id': prov_playlist_id,
            'track_ids': ",".join(prov_track_ids)
        }
        return await self.__get_data('playlist/addTracks', params)

    async def remove_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        ''' remove track(s) from playlist '''
        playlist_track_ids = []
        params = {'playlist_id': prov_playlist_id, 'extra': 'tracks'}
        for track in await self.__get_all_items("playlist/get", params, key='tracks', limit=0):
            if track['id'] in prov_track_ids:
                playlist_track_ids.append(track['playlist_track_id'])
        params = {'playlist_id': prov_playlist_id, 'track_ids': ",".join(playlist_track_ids)}
        return await self.__get_data('playlist/deleteTracks', params)
    
    async def get_stream_details(self, track_id):
        ''' return the content details for the given track when it will be streamed'''
        streamdetails = None
        for format_id in [27, 7, 6, 5]:
            # it seems that simply requesting for highest available quality does not work
            # from time to time the api response is empty for this request ?!
            params = {'format_id': format_id, 'track_id': track_id, 'intent': 'stream'}
            streamdetails = await self.__get_data('track/getFileUrl', params, sign_request=True, ignore_cache=True)
            if streamdetails and streamdetails.get('url'):
                break
        if not streamdetails or not streamdetails.get('url'):
            LOGGER.error("Unable to retrieve stream url for track %s" % track_id)
            return None
        return {
            "type": "url",
            "path": streamdetails['url'],
            "content_type": streamdetails['mime_type'].split('/')[1],
            "sample_rate": int(streamdetails['sampling_rate']*1000),
            "bit_depth": streamdetails['bit_depth'],
            "details": streamdetails # we need these details for reporting playback
        }

    async def mass_event(self, msg, msg_details):
        ''' received event from mass '''
        # TODO: need to figure out if the streamed track is purchased
        if msg == "streaming_started" and msg_details['provider'] == self.prov_id:
            # report streaming started to qobuz
            LOGGER.debug("streaming_started %s" % msg_details["track_id"])
            device_id = self.__user_auth_info["user"]["device"]["id"]
            credential_id = self.__user_auth_info["user"]["credential"]["id"]
            user_id = self.__user_auth_info["user"]["id"]
            format_id = msg_details["details"]["format_id"]
            timestamp = int(time.time())
            events=[{"online": True, "sample": False, "intent": "stream", "device_id": device_id, 
                "track_id": msg_details["track_id"], "purchase": False, "date": timestamp,
                "credential_id": credential_id, "user_id": user_id, "local": False, "format_id":format_id}]
            await self.__post_data("track/reportStreamingStart", data=events)
        elif msg == "streaming_ended" and msg_details['provider'] == self.prov_id:
            # report streaming ended to qobuz
            LOGGER.debug("streaming_ended %s - seconds played: %s" %(msg_details["track_id"], msg_details["seconds"]) )
            device_id = self.__user_auth_info["user"]["device"]["id"]
            credential_id = self.__user_auth_info["user"]["credential"]["id"]
            user_id = self.__user_auth_info["user"]["id"]
            format_id = msg_details["details"]["format_id"]
            timestamp = int(time.time())
            events=[{"online": True, "sample": False, "intent": "stream", "device_id": device_id, 
                "track_id": msg_details["track_id"], "purchase": False, "date": timestamp, "duration": msg_details["seconds"],
                "credential_id": credential_id, "user_id": user_id, "local": False, "format_id":format_id}]
            await self.__post_data("track/reportStreamingStart", data=events)
    
    async def __parse_artist(self, artist_obj):
        ''' parse qobuz artist object to generic layout '''
        artist = Artist()
        if not artist_obj.get('id'):
            return None
        artist.item_id = artist_obj['id']
        artist.provider = self.prov_id
        artist.provider_ids.append({
            "provider": self.prov_id,
            "item_id": artist_obj['id']
        })
        artist.name = artist_obj['name']
        if artist_obj.get('image'):
            for key in ['extralarge', 'large', 'medium', 'small']:
                if artist_obj['image'].get(key):
                    if not '2a96cbd8b46e442fc41c2b86b821562f' in artist_obj['image'][key]:
                        artist.metadata["image"] = artist_obj['image'][key]
                        break
        if artist_obj.get('biography'):
            artist.metadata["biography"] = artist_obj['biography'].get('content','')
        if artist_obj.get('url'):
            artist.metadata["qobuz_url"] = artist_obj['url']
        return artist

    async def __parse_album(self, album_obj):
        ''' parse qobuz album object to generic layout '''
        album = Album()
        if not album_obj.get('id') or not album_obj["streamable"] or not album_obj["displayable"]:
            # some safety checks
            LOGGER.warning("invalid/unavailable album found: %s" % album_obj.get('id'))
            return None
        album.item_id = album_obj['id']
        album.provider = self.prov_id
        album.provider_ids.append({
            "provider": self.prov_id,
            "item_id": album_obj['id'],
            "details": "%skHz %sbit" %(album_obj['maximum_sampling_rate'], album_obj['maximum_bit_depth'])
        })
        album.name, album.version = parse_track_title(album_obj['title'])
        album.artist = await self.__parse_artist(album_obj['artist'])
        if not album.artist:
            raise Exception("No album artist ! %s" % album_obj)
        if album_obj.get('product_type','') == 'single':
            album.albumtype = AlbumType.Single
        elif album_obj.get('product_type','') == 'compilation' or 'Various' in album_obj['artist']['name']:
            album.albumtype = AlbumType.Compilation
        else:
            album.albumtype = AlbumType.Album
        if 'genre' in album_obj:
            album.tags = [album_obj['genre']['name']]
        if album_obj.get('image'):
            for key in ['extralarge', 'large', 'medium', 'small']:
                if album_obj['image'].get(key):
                    album.metadata["image"] = album_obj['image'][key]
                    break
        album.external_ids.append({ "upc": album_obj['upc'] })
        if 'label' in album_obj:
            album.labels = album_obj['label']['name'].split('/')
        if album_obj.get('released_at'):
            album.year = datetime.datetime.fromtimestamp(album_obj['released_at']).year
        if album_obj.get('copyright'):
            album.metadata["copyright"] = album_obj['copyright']
        if album_obj.get('hires'):
            album.metadata["hires"] = "true"
        if album_obj.get('url'):
            album.metadata["qobuz_url"] = album_obj['url']
        if album_obj.get('description'):
            album.metadata["description"] = album_obj['description']
        return album

    async def __parse_track(self, track_obj):
        ''' parse qobuz track object to generic layout '''
        track = Track()
        if not track_obj.get('id') or not track_obj["streamable"] or not track_obj["displayable"]:
            # some safety checks
            LOGGER.warning("invalid/unavailable track found: %s - %s" % (track_obj.get('id'), track_obj.get('name')))
            return None
        track.item_id = track_obj['id']
        track.provider = self.prov_id
        if track_obj.get('performer') and not 'Various ' in track_obj['performer']:
            artist = await self.__parse_artist(track_obj['performer'])
            if not artist:
                artist = self.get_artist(track_obj['performer']['id'])
            if artist:
                track.artists.append(artist)
        if not track.artists:
            # try to grab artist from album
            if track_obj.get('album') and track_obj['album'].get('artist') and not 'Various ' in track_obj['album']['artist']:
                artist = await self.__parse_artist(track_obj['album']['artist'])
                if artist:
                    track.artists.append(artist)
        if not track.artists:
            # last resort: parse from performers string
            for performer_str in track_obj['performers'].split(' - '):
                role = performer_str.split(', ')[1]
                name = performer_str.split(', ')[0]
                if 'artist' in role.lower():
                    artist = Artist()
                    artist.name = name
                    artist.item_id = name
                track.artists.append(artist)
        # TODO: fix grabbing composer from details
        track.name, track.version = parse_track_title(track_obj['title'])
        if not track.version and track_obj['version']:
            track.version = track_obj['version']
        track.duration = track_obj['duration']
        if 'album' in track_obj:
            album = await self.__parse_album(track_obj['album'])
            if album:
                track.album = album
        track.disc_number = track_obj['media_number']
        track.track_number = track_obj['track_number']
        if track_obj.get('hires'):
            track.metadata["hires"] = "true"
        if track_obj.get('url'):
            track.metadata["qobuz_url"] = track_obj['url']
        if track_obj.get('isrc'):
            track.external_ids.append({
                "isrc": track_obj['isrc']
            })
        if track_obj.get('performers'):
            track.metadata["performers"] = track_obj['performers']
        if track_obj.get('copyright'):
            track.metadata["copyright"] = track_obj['copyright']
        # get track quality
        if track_obj['maximum_sampling_rate'] > 192:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_4
        elif track_obj['maximum_sampling_rate'] > 96:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_3
        elif track_obj['maximum_sampling_rate'] > 48:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_2
        elif track_obj['maximum_bit_depth'] > 16:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_1
        elif track_obj.get('format_id',0) == 5:
            quality = TrackQuality.LOSSY_AAC
        else:
            quality = TrackQuality.FLAC_LOSSLESS
        track.provider_ids.append({
            "provider": self.prov_id,
            "item_id": track_obj['id'],
            "quality": quality,
            "details": "%skHz %sbit" %(track_obj['maximum_sampling_rate'], track_obj['maximum_bit_depth'])
        })
        return track

    async def __parse_playlist(self, playlist_obj):
        ''' parse qobuz playlist object to generic layout '''
        playlist = Playlist()
        if not playlist_obj.get('id'):
            return None
        playlist.item_id = playlist_obj['id']
        playlist.provider = self.prov_id
        playlist.provider_ids.append({
            "provider": self.prov_id,
            "item_id": playlist_obj['id']
        })
        playlist.name = playlist_obj['name']
        playlist.owner = playlist_obj['owner']['name']
        playlist.is_editable = playlist_obj['owner']['id'] == self.__user_auth_info["user"]["id"] or playlist_obj['is_collaborative']
        if playlist_obj.get('images300'):
            playlist.metadata["image"] = playlist_obj['images300'][0]
        if playlist_obj.get('url'):
            playlist.metadata["qobuz_url"] = playlist_obj['url']
        return playlist

    async def __auth_token(self):
        ''' login to qobuz and store the token'''
        if self.__user_auth_info:
            return self.__user_auth_info["user_auth_token"]
        params = { "username": self.__username, "password": self.__password, "device_manufacturer_id": "music_assistant"}
        details = await self.__get_data("user/login", params, ignore_cache=True)
        self.__user_auth_info = details
        LOGGER.info("Succesfully logged in to Qobuz as %s" % (details["user"]["display_name"]))
        return details["user_auth_token"]

    async def __get_all_items(self, endpoint, params={}, key="playlists", limit=0, offset=0, cache_checksum=None):
        ''' get all items from a paged list '''
        if not cache_checksum:
            params["limit"] = 1
            params["offset"] = 0
            cache_checksum = await self.__get_data(endpoint, params, ignore_cache=True)
            cache_checksum = cache_checksum[key]["total"]
        if limit:
            # partial listing
            params["limit"] = limit
            params["offset"] = offset
            result = await self.__get_data(endpoint, params=params, cache_checksum=cache_checksum)
            return result[key]["items"]
        else:
            # full listing
            offset = 0
            total_items = 1
            count = 0
            items = []
            while count < total_items:
                params["limit"] = 200
                params["offset"] = offset
                result = await self.__get_data(endpoint, params=params, cache_checksum=cache_checksum)
                if result and key in result:
                    total_items = result[key]["total"]
                    offset += 200
                    count += len(result[key]["items"])
                    items += result[key]["items"]
                else:
                    LOGGER.error("failed to retrieve items for %s (%s) --> %s" %(endpoint, params, result))
                    break
            return items

    @use_cache(7)
    async def __get_data(self, endpoint, params={}, sign_request=False, ignore_cache=False, cache_checksum=None):
        ''' get data from api'''
        url = "http://www.qobuz.com/api.json/0.2/%s" % endpoint
        headers = {"X-App-Id": get_app_var(0)}
        if endpoint != 'user/login':
            headers["X-User-Auth-Token"] = await self.__auth_token()
        if sign_request:
            signing_data = "".join(endpoint.split('/'))
            keys = list(params.keys())
            keys.sort()
            for key in keys:
                signing_data += "%s%s" %(key, params[key])
            request_ts = str(time.time())
            request_sig = signing_data + request_ts + get_app_var(1)
            request_sig = str(hashlib.md5(request_sig.encode()).hexdigest())
            params["request_ts"] = request_ts
            params["request_sig"] = request_sig
            params["app_id"] = get_app_var(0)
            params["user_auth_token"] = await self.__auth_token()
        try:
            async with self.throttler:
                async with self.http_session.get(url, headers=headers, params=params) as response:
                    result = await response.json()
                    if not result or 'error' in result:
                        LOGGER.error(url)
                        LOGGER.debug(params)
                        LOGGER.debug(result)
                        return None
                    return result
        except Exception as exc:
            LOGGER.exception(exc)
            return None

    async def __post_data(self, endpoint, params={}, data={}):
        ''' post data to api'''
        url = "http://www.qobuz.com/api.json/0.2/%s" % endpoint
        params["app_id"] = QOBUZ_APP_ID
        params["user_auth_token"] = await self.__auth_token()
        async with self.http_session.post(url, params=params, json=data) as response:
            result = await response.json()
            if not result or 'error' in result:
                LOGGER.error(url)
                LOGGER.debug(params)
                LOGGER.debug(result)
                result = None
            return result