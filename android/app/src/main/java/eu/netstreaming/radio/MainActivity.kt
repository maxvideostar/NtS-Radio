package eu.netstreaming.radio

import android.content.ComponentName
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.session.MediaController
import androidx.media3.session.SessionCommand
import androidx.media3.session.SessionToken
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.card.MaterialCardView
import com.google.common.util.concurrent.ListenableFuture
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity() {

    private var controllerFuture: ListenableFuture<MediaController>? = null
    private var controller: MediaController? = null
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private lateinit var adapter: StationAdapter
    private lateinit var swipe: SwipeRefreshLayout
    private lateinit var empty: TextView
    private lateinit var miniArt: ImageView
    private lateinit var miniTitle: TextView
    private lateinit var miniSub: TextView
    private lateinit var miniPlay: ImageButton

    private val refresher = object : Runnable {
        override fun run() {
            load(false)
            main.postDelayed(this, 20_000)
        }
    }

    private val listener = object : Player.Listener {
        override fun onEvents(player: Player, events: Player.Events) = updateMini()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val toolbar = findViewById<MaterialToolbar>(R.id.toolbar)
        toolbar.inflateMenu(R.menu.main)
        toolbar.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                R.id.action_sleep -> sleepDialog()
                R.id.action_quality -> qualityDialog()
                R.id.action_server -> serverDialog()
                R.id.action_about -> aboutDialog()
            }
            true
        }

        swipe = findViewById(R.id.swipe)
        empty = findViewById(R.id.empty)
        miniArt = findViewById(R.id.mini_art)
        miniTitle = findViewById(R.id.mini_title)
        miniSub = findViewById(R.id.mini_sub)
        miniPlay = findViewById(R.id.mini_play)
        miniTitle.isSelected = true

        adapter = StationAdapter { play(it) }
        val list = findViewById<RecyclerView>(R.id.list)
        list.layoutManager = LinearLayoutManager(this)
        list.adapter = adapter

        swipe.setColorSchemeColors(ContextCompat.getColor(this, R.color.accent))
        swipe.setOnRefreshListener { load(true) }

        findViewById<View>(R.id.mini).setOnClickListener {
            if (controller?.currentMediaItem != null) openPlayer()
        }
        miniPlay.setOnClickListener {
            val c = controller ?: return@setOnClickListener
            if (c.isPlaying || c.playbackState == Player.STATE_BUFFERING) {
                c.stop()
            } else if (c.mediaItemCount > 0) {
                c.prepare(); c.play()
            } else {
                val last = Stations.prefs(this).getString("last", null)
                (Stations.find(last) ?: Stations.list.firstOrNull())?.let { play(it) }
            }
        }

        if (Stations.list.isNotEmpty()) adapter.submit(Stations.list)
    }

    override fun onStart() {
        super.onStart()
        val token = SessionToken(this, ComponentName(this, PlaybackService::class.java))
        val f = MediaController.Builder(this, token).buildAsync()
        controllerFuture = f
        f.addListener({
            controller = runCatching { f.get() }.getOrNull()
            controller?.addListener(listener)
            updateMini()
        }, ContextCompat.getMainExecutor(this))
        main.post(refresher)
    }

    override fun onStop() {
        main.removeCallbacks(refresher)
        controller?.removeListener(listener)
        controllerFuture?.let { MediaController.releaseFuture(it) }
        controller = null
        super.onStop()
    }

    override fun onDestroy() {
        io.shutdown()
        super.onDestroy()
    }

    private fun load(user: Boolean) {
        if (user) swipe.isRefreshing = true
        io.execute {
            val res = runCatching { Stations.fetch(this) }
            main.post {
                swipe.isRefreshing = false
                res.onSuccess { adapter.submit(it); empty.visibility = if (it.isEmpty()) View.VISIBLE else View.GONE }
                res.onFailure {
                    if (adapter.itemCount == 0) empty.visibility = View.VISIBLE
                    if (user) Toast.makeText(this, "Eroare: ${it.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun play(s: Station) {
        val c = controller ?: return
        c.setMediaItem(Stations.toMediaItem(s))
        c.prepare()
        c.play()
        Stations.prefs(this).edit().putString("last", s.slug).apply()
        updateMini()
        openPlayer()
    }

    private fun openPlayer() {
        startActivity(Intent(this, PlayerActivity::class.java))
        overridePendingTransition(android.R.anim.fade_in, android.R.anim.fade_out)
    }

    private fun updateMini() {
        val c = controller
        val item = c?.currentMediaItem
        if (c == null || item == null) {
            miniTitle.setText(R.string.tap_to_play)
            miniSub.text = ""
            miniPlay.setImageResource(R.drawable.ic_play)
            adapter.setPlaying(null)
            return
        }
        val st = Stations.find(item.mediaId)
        val md: MediaMetadata = c.mediaMetadata
        val stationName = st?.name ?: item.mediaMetadata.title?.toString() ?: ""
        val song = md.title?.toString()?.takeIf { it.isNotBlank() && it != stationName }
        miniTitle.text = song ?: stationName
        miniSub.text = when {
            c.playbackState == Player.STATE_BUFFERING -> getString(R.string.connecting)
            song != null -> stationName
            else -> st?.genre ?: ""
        }
        val active = c.isPlaying || c.playbackState == Player.STATE_BUFFERING
        miniPlay.setImageResource(if (active) R.drawable.ic_stop else R.drawable.ic_play)
        ImageLoader.load(st?.logo ?: item.mediaMetadata.artworkUri?.toString(), miniArt, R.mipmap.ic_launcher)
        adapter.setPlaying(if (active) item.mediaId else null)
    }

    // ---------------- dialoguri ----------------
    private fun sleepDialog() {
        val opts = arrayOf("Dezactivat", "15 minute", "30 minute", "45 minute", "60 minute", "90 minute")
        val mins = intArrayOf(0, 15, 30, 45, 60, 90)
        AlertDialog.Builder(this).setTitle(R.string.sleep).setItems(opts) { _, i ->
            PlayerActivity.setSleep(this, controller, mins[i])
        }.show()
    }

    private fun qualityDialog() {
        val opts = arrayOf("Calitate maximă", "Economic (consum mic de date)")
        val cur = if (Stations.quality(this) == "high") 0 else 1
        AlertDialog.Builder(this).setTitle(R.string.quality).setSingleChoiceItems(opts, cur) { d, i ->
            Stations.prefs(this).edit().putString("quality", if (i == 0) "high" else "low").apply()
            d.dismiss()
            load(true)
        }.show()
    }

    private fun serverDialog() {
        val input = EditText(this).apply {
            setText(Stations.server(this@MainActivity))
            setSingleLine()
            setPadding(48, 32, 48, 32)
        }
        AlertDialog.Builder(this).setTitle(R.string.server).setView(input)
            .setPositiveButton("Salvează") { _, _ ->
                val v = input.text.toString().trim().trimEnd('/')
                Stations.prefs(this).edit().putString("server", v.ifBlank { BuildConfig.DEFAULT_SERVER }).apply()
                load(true)
            }
            .setNeutralButton("Implicit") { _, _ ->
                Stations.prefs(this).edit().remove("server").apply(); load(true)
            }
            .setNegativeButton("Anulează", null).show()
    }

    private fun aboutDialog() {
        AlertDialog.Builder(this).setTitle(R.string.app_name)
            .setMessage("Versiunea ${BuildConfig.VERSION_NAME}\n\nnetstreaming.eu – streaming services\n\nServer: ${Stations.server(this)}")
            .setPositiveButton("OK", null).show()
    }
}

class StationAdapter(private val onClick: (Station) -> Unit) : RecyclerView.Adapter<StationAdapter.VH>() {
    private var items: List<Station> = emptyList()
    private var playing: String? = null

    fun submit(list: List<Station>) { items = list; notifyDataSetChanged() }

    fun setPlaying(slug: String?) {
        if (slug != playing) { playing = slug; notifyDataSetChanged() }
    }

    class VH(v: View) : RecyclerView.ViewHolder(v) {
        val card: MaterialCardView = v.findViewById(R.id.card)
        val logo: ImageView = v.findViewById(R.id.logo)
        val name: TextView = v.findViewById(R.id.name)
        val now: TextView = v.findViewById(R.id.now)
        val genre: TextView = v.findViewById(R.id.genre)
        val live: TextView = v.findViewById(R.id.live)
        val playingIcon: ImageView = v.findViewById(R.id.playing)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
        VH(LayoutInflater.from(parent.context).inflate(R.layout.item_station, parent, false))

    override fun getItemCount() = items.size

    override fun onBindViewHolder(h: VH, pos: Int) {
        val s = items[pos]
        h.name.text = s.name
        h.now.text = if (s.title.isNotBlank() && s.title != s.name) "♪ ${s.title}" else if (s.onAir) "" else "Emisie oprită"
        h.genre.text = s.genre
        h.genre.visibility = if (s.genre.isBlank()) View.GONE else View.VISIBLE
        h.live.visibility = if (s.live) View.VISIBLE else View.GONE
        val isPlaying = s.slug == playing
        h.playingIcon.visibility = if (isPlaying) View.VISIBLE else View.GONE
        h.card.strokeWidth = if (isPlaying) (2 * h.card.resources.displayMetrics.density).toInt() else 0
        ImageLoader.load(s.logo, h.logo, R.mipmap.ic_launcher)
        h.card.setOnClickListener { onClick(s) }
    }
}
