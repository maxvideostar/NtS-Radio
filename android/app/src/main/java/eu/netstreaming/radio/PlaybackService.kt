package eu.netstreaming.radio

import android.app.PendingIntent
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.exoplayer.upstream.DefaultLoadErrorHandlingPolicy
import androidx.media3.session.LibraryResult
import androidx.media3.session.MediaLibraryService
import androidx.media3.session.MediaSession
import androidx.media3.session.SessionCommand
import androidx.media3.session.SessionError
import androidx.media3.session.SessionResult
import com.google.common.collect.ImmutableList
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import com.google.common.util.concurrent.SettableFuture
import java.util.concurrent.Executors

@UnstableApi
class PlaybackService : MediaLibraryService() {

    companion object {
        const val ROOT = "root"
        const val CMD_SLEEP = "eu.netstreaming.radio.SLEEP"
        const val ARG_MINUTES = "minutes"
    }

    private var session: MediaLibrarySession? = null
    private lateinit var player: ExoPlayer
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var retries = 0
    private val sleepRunnable = Runnable { player.stop() }

    override fun onCreate() {
        super.onCreate()
        val http = DefaultHttpDataSource.Factory()
            .setUserAgent(Stations.UA)
            .setAllowCrossProtocolRedirects(true)
            .setConnectTimeoutMs(15000)
            .setReadTimeoutMs(30000)
        val sources = DefaultMediaSourceFactory(DefaultDataSource.Factory(this, http))
            .setLoadErrorHandlingPolicy(DefaultLoadErrorHandlingPolicy(10))

        player = ExoPlayer.Builder(this)
            .setMediaSourceFactory(sources)
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(C.USAGE_MEDIA)
                    .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
                    .build(),
                true
            )
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()

        player.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                // stream live: reîncearcă automat (rețea mobilă, tunel, schimbare Wi-Fi/4G)
                if (retries < 30) {
                    retries++
                    main.postDelayed({
                        if (player.mediaItemCount > 0) {
                            player.prepare()
                            player.play()
                        }
                    }, if (retries < 3) 2000L else 5000L)
                }
            }

            override fun onIsPlayingChanged(isPlaying: Boolean) {
                if (isPlaying) retries = 0
            }

            override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
                mediaItem?.mediaId?.let { Stations.prefs(this@PlaybackService).edit().putString("last", it).apply() }
            }
        })

        val launch = packageManager.getLaunchIntentForPackage(packageName) ?: Intent(this, MainActivity::class.java)
        val pi = PendingIntent.getActivity(
            this, 0, launch, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        session = MediaLibrarySession.Builder(this, player, Callback())
            .setSessionActivity(pi)
            .build()

        io.execute { runCatching { Stations.fetch(this) } }
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaLibrarySession? = session

    override fun onTaskRemoved(rootIntent: Intent?) {
        if (!player.playWhenReady || player.mediaItemCount == 0) stopSelf()
    }

    override fun onDestroy() {
        main.removeCallbacksAndMessages(null)
        session?.run {
            player.release()
            release()
        }
        session = null
        io.shutdown()
        super.onDestroy()
    }

    private fun stationsOrFetch(): List<Station> {
        if (Stations.list.isEmpty()) runCatching { Stations.fetch(this) }
        return Stations.list
    }

    private inner class Callback : MediaLibrarySession.Callback {

        override fun onConnect(
            session: MediaSession,
            controller: MediaSession.ControllerInfo
        ): MediaSession.ConnectionResult {
            val cmds = MediaSession.ConnectionResult.DEFAULT_SESSION_AND_LIBRARY_COMMANDS.buildUpon()
                .add(SessionCommand(CMD_SLEEP, Bundle.EMPTY))
                .build()
            return MediaSession.ConnectionResult.AcceptedResultBuilder(session)
                .setAvailableSessionCommands(cmds)
                .build()
        }

        override fun onCustomCommand(
            session: MediaSession,
            controller: MediaSession.ControllerInfo,
            customCommand: SessionCommand,
            args: Bundle
        ): ListenableFuture<SessionResult> {
            if (customCommand.customAction == CMD_SLEEP) {
                main.removeCallbacks(sleepRunnable)
                val min = args.getInt(ARG_MINUTES, 0)
                if (min > 0) main.postDelayed(sleepRunnable, min * 60_000L)
                return Futures.immediateFuture(SessionResult(SessionResult.RESULT_SUCCESS))
            }
            return super.onCustomCommand(session, controller, customCommand, args)
        }

        override fun onGetLibraryRoot(
            session: MediaLibrarySession,
            browser: MediaSession.ControllerInfo,
            params: LibraryParams?
        ): ListenableFuture<LibraryResult<MediaItem>> {
            val extras = Bundle().apply {
                // Android Auto: afișare tip grilă
                putInt("android.media.browse.CONTENT_STYLE_PLAYABLE_HINT", 2)
                putInt("android.media.browse.CONTENT_STYLE_BROWSABLE_HINT", 2)
            }
            val root = MediaItem.Builder()
                .setMediaId(ROOT)
                .setMediaMetadata(
                    MediaMetadata.Builder()
                        .setTitle("NetStreaming")
                        .setIsBrowsable(true)
                        .setIsPlayable(false)
                        .setMediaType(MediaMetadata.MEDIA_TYPE_FOLDER_RADIO_STATIONS)
                        .build()
                )
                .build()
            return Futures.immediateFuture(
                LibraryResult.ofItem(root, LibraryParams.Builder().setExtras(extras).build())
            )
        }

        override fun onGetChildren(
            session: MediaLibrarySession,
            browser: MediaSession.ControllerInfo,
            parentId: String,
            page: Int,
            pageSize: Int,
            params: LibraryParams?
        ): ListenableFuture<LibraryResult<ImmutableList<MediaItem>>> {
            val f = SettableFuture.create<LibraryResult<ImmutableList<MediaItem>>>()
            io.execute {
                val list = try { Stations.fetch(this@PlaybackService) } catch (e: Exception) { Stations.list }
                f.set(LibraryResult.ofItemList(ImmutableList.copyOf(list.map { Stations.toMediaItem(it) }), params))
            }
            return f
        }

        override fun onGetItem(
            session: MediaLibrarySession,
            browser: MediaSession.ControllerInfo,
            mediaId: String
        ): ListenableFuture<LibraryResult<MediaItem>> {
            val f = SettableFuture.create<LibraryResult<MediaItem>>()
            io.execute {
                val s = stationsOrFetch().firstOrNull { it.slug == mediaId }
                f.set(
                    if (s != null) LibraryResult.ofItem(Stations.toMediaItem(s), null)
                    else LibraryResult.ofError(SessionError.ERROR_BAD_VALUE)
                )
            }
            return f
        }

        override fun onSearch(
            session: MediaLibrarySession,
            browser: MediaSession.ControllerInfo,
            query: String,
            params: LibraryParams?
        ): ListenableFuture<LibraryResult<Void>> {
            io.execute {
                val n = stationsOrFetch().count { it.name.contains(query, true) }
                session.notifySearchResultChanged(browser, query, n, params)
            }
            return Futures.immediateFuture(LibraryResult.ofVoid())
        }

        override fun onGetSearchResult(
            session: MediaLibrarySession,
            browser: MediaSession.ControllerInfo,
            query: String,
            page: Int,
            pageSize: Int,
            params: LibraryParams?
        ): ListenableFuture<LibraryResult<ImmutableList<MediaItem>>> {
            val f = SettableFuture.create<LibraryResult<ImmutableList<MediaItem>>>()
            io.execute {
                val res = stationsOrFetch().filter { it.name.contains(query, true) }.map { Stations.toMediaItem(it) }
                f.set(LibraryResult.ofItemList(ImmutableList.copyOf(res), params))
            }
            return f
        }

        override fun onAddMediaItems(
            mediaSession: MediaSession,
            controller: MediaSession.ControllerInfo,
            mediaItems: MutableList<MediaItem>
        ): ListenableFuture<MutableList<MediaItem>> {
            // Android Auto / asistentul trimit doar ID-ul postului: completăm adresa stream-ului
            val f = SettableFuture.create<MutableList<MediaItem>>()
            io.execute {
                val list = stationsOrFetch()
                val q = mediaItems.firstOrNull()?.requestMetadata?.searchQuery
                val res = mediaItems.map { mi ->
                    val s = list.firstOrNull { it.slug == mi.mediaId }
                        ?: q?.let { qq -> list.firstOrNull { it.name.contains(qq, true) } }
                    if (s != null) Stations.toMediaItem(s) else mi
                }.toMutableList()
                f.set(res)
            }
            return f
        }

        override fun onPlaybackResumption(
            mediaSession: MediaSession,
            controller: MediaSession.ControllerInfo
        ): ListenableFuture<MediaSession.MediaItemsWithStartPosition> {
            val f = SettableFuture.create<MediaSession.MediaItemsWithStartPosition>()
            io.execute {
                val last = Stations.prefs(this@PlaybackService).getString("last", null)
                val list = stationsOrFetch()
                val s = list.firstOrNull { it.slug == last } ?: list.firstOrNull()
                if (s == null) f.setException(IllegalStateException("fără posturi"))
                else f.set(MediaSession.MediaItemsWithStartPosition(listOf(Stations.toMediaItem(s)), 0, C.TIME_UNSET))
            }
            return f
        }
    }
}
