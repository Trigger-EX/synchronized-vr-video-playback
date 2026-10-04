package com.syncvr.operator

import android.app.Activity
import android.app.AlertDialog
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import com.syncvr.player.core.operator.LibraryVideo
import com.syncvr.player.core.operator.OperatorApi
import com.syncvr.player.core.operator.OperatorDevice
import com.syncvr.player.core.operator.OperatorEvent
import com.syncvr.player.core.operator.OperatorUiState
import com.syncvr.player.core.operator.OperatorViewModel
import com.syncvr.player.core.operator.ServerEndpoint
import com.syncvr.player.core.operator.formatBytes
import com.syncvr.player.core.operator.formatTime
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

/**
 * Dashboard: status line, transport controls, and three lists (headsets, library, events).
 * Everything that decides what to send lives in OperatorViewModel (:core); this class only draws
 * its state and forwards taps.
 */
class DashboardActivity : Activity() {
    companion object {
        const val EXTRA_HOST = "host"
        const val EXTRA_PORT = "port"
        const val EXTRA_PASSWORD = "password"
        private const val POLL_MS = 1000L
        private const val SEEK_STEPS = 1000
        private val SELECTED = Color.rgb(209, 231, 255)
    }

    private val ui = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private lateinit var vm: OperatorViewModel
    private var state = OperatorUiState()
    private var polling = false

    private lateinit var status: TextView
    private lateinit var banner: TextView
    private lateinit var scope: TextView
    private lateinit var seekBar: SeekBar
    private lateinit var seekLabel: TextView
    private lateinit var volumeBar: SeekBar
    private lateinit var libraryHeader: TextView
    private lateinit var devicesList: ListView
    private lateinit var libraryPage: LinearLayout
    private lateinit var libraryList: ListView
    private lateinit var eventsList: ListView
    private var seekDragging = false

    private val devicesAdapter = DevicesAdapter()
    private val libraryAdapter = LibraryAdapter()
    private val eventsAdapter = EventsAdapter()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val endpoint = ServerEndpoint(
            intent.getStringExtra(EXTRA_HOST) ?: "",
            intent.getIntExtra(EXTRA_PORT, 8080),
            intent.getStringExtra(EXTRA_PASSWORD) ?: "",
        )
        title = endpoint.label
        vm = OperatorViewModel(OperatorApi(endpoint), { task -> if (!worker.isShutdown) worker.execute(task) }) { s ->
            runOnUiThread { render(s) }
        }
        setContentView(buildLayout())
    }

    override fun onResume() {
        super.onResume()
        polling = true
        poll()
    }

    override fun onPause() {
        polling = false
        ui.removeCallbacksAndMessages(null)
        super.onPause()
    }

    override fun onDestroy() {
        worker.shutdownNow()
        super.onDestroy()
    }

    private fun poll() {
        if (!polling) return
        vm.refresh()
        ui.postDelayed({ poll() }, POLL_MS)
    }

    // ------------------------------------------------------------------ layout

    private fun buildLayout(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(8), dp(8), dp(8), dp(8))
        }
        status = label("Connecting...", 15f, bold = true)
        banner = label("", 13f).apply { setTextColor(Color.rgb(176, 0, 32)); visibility = View.GONE }
        root.addView(status, matchWrap())
        root.addView(banner, matchWrap())

        // Transport.
        scope = label("", 13f)
        root.addView(scope, matchWrap())
        root.addView(row(
            button("Play") { vm.play() },
            button("Pause") { vm.pause() },
            button("Stop") { vm.stop() },
        ), matchWrap())
        seekLabel = label("0:00 / 0:00", 13f)
        seekBar = SeekBar(this).apply {
            max = SEEK_STEPS
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(bar: SeekBar, progress: Int, fromUser: Boolean) {
                    if (fromUser) seekLabel.text = seekText(progress / SEEK_STEPS.toDouble() * state.seekDuration)
                }
                override fun onStartTrackingTouch(bar: SeekBar) { seekDragging = true }
                override fun onStopTrackingTouch(bar: SeekBar) {
                    seekDragging = false
                    if (state.seekDuration > 0) vm.seekTo(bar.progress / SEEK_STEPS.toDouble() * state.seekDuration)
                }
            })
        }
        root.addView(seekBar, matchWrap())
        root.addView(seekLabel, matchWrap())
        root.addView(row(
            button("-10 s") { vm.seekBy(-10.0) },
            button("+10 s") { vm.seekBy(10.0) },
            button("Resync") { vm.resync() },
            button("Identify") { vm.identify() },
        ), matchWrap())
        root.addView(label("Volume", 13f), matchWrap())
        volumeBar = SeekBar(this).apply {
            max = 100
            progress = 100
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(bar: SeekBar, progress: Int, fromUser: Boolean) {}
                override fun onStartTrackingTouch(bar: SeekBar) {}
                override fun onStopTrackingTouch(bar: SeekBar) { vm.setVolume(bar.progress / 100.0) }
            })
        }
        root.addView(volumeBar, matchWrap())

        // Pages.
        devicesList = ListView(this).apply {
            adapter = devicesAdapter
            setOnItemClickListener { _, _, position, _ -> devicesAdapter.item(position)?.let { vm.toggleSelected(it.id) } }
            setOnItemLongClickListener { _, _, position, _ -> devicesAdapter.item(position)?.let { editDevice(it) }; true }
        }
        libraryHeader = label("", 13f)
        libraryList = ListView(this).apply {
            adapter = libraryAdapter
            setOnItemClickListener { _, _, position, _ -> libraryAdapter.item(position)?.let { vm.selectVideo(it.name) } }
        }
        libraryPage = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(libraryHeader, matchWrap())
            addView(row(
                button("Load") { vm.load() },
                button("Play video") { vm.playVideo() },
            ), matchWrap())
            addView(row(
                button("Send to headsets") { vm.syncContent() },
                button("Cancel sends") { vm.cancelDownloads() },
                button("Rescan") { vm.rescanLibrary() },
            ), matchWrap())
            addView(libraryList, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        }
        eventsList = ListView(this).apply { adapter = eventsAdapter }

        val pages = FrameLayout(this).apply {
            addView(devicesList, FrameLayout.LayoutParams(-1, -1))
            addView(libraryPage, FrameLayout.LayoutParams(-1, -1))
            addView(eventsList, FrameLayout.LayoutParams(-1, -1))
        }
        fun show(page: View) {
            devicesList.visibility = if (page === devicesList) View.VISIBLE else View.GONE
            libraryPage.visibility = if (page === libraryPage) View.VISIBLE else View.GONE
            eventsList.visibility = if (page === eventsList) View.VISIBLE else View.GONE
        }
        show(devicesList)
        root.addView(row(
            button("Headsets") { show(devicesList) },
            button("Library") { show(libraryPage) },
            button("Events") { show(eventsList) },
            button("Servers") { finish() },
        ), matchWrap())
        root.addView(pages, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        return root
    }

    // ------------------------------------------------------------------ render

    private fun render(s: OperatorUiState) {
        state = s
        val snap = s.snapshot
        status.text = if (snap == null) "Connecting..." else
            "${snap.serverName}: ${s.onlineCount}/${snap.devices.size} online, ${s.playingCount} playing"
        banner.visibility = if (s.connectionError != null) View.VISIBLE else View.GONE
        banner.text = when {
            s.unauthorized -> "Wrong or missing password. Go back to Servers and reconnect."
            s.connectionError != null -> "Connection lost: ${s.connectionError}. Retrying..."
            else -> ""
        }
        scope.text = if (s.selected.isEmpty()) "Controls apply to: all headsets (tap rows to choose)"
        else "Controls apply to: ${s.selected.size} selected headset(s)"
        if (!seekDragging) {
            val dur = s.seekDuration
            seekBar.progress = if (dur > 0) ((s.seekPosition / dur) * SEEK_STEPS).toInt().coerceIn(0, SEEK_STEPS) else 0
            seekLabel.text = seekText(s.seekPosition)
        }
        libraryHeader.text = if (snap?.downloads?.busy == true)
            "Sending: ${snap.downloads.active.size} active, ${snap.downloads.queued.size} waiting"
        else "Tap a video to choose it, then Load or Play video."
        devicesAdapter.submit(s)
        libraryAdapter.submit(s)
        eventsAdapter.submit(s)
        s.notice?.let {
            Toast.makeText(this, it, if (s.noticeIsError) Toast.LENGTH_LONG else Toast.LENGTH_SHORT).show()
            vm.consumeNotice()
        }
    }

    private fun seekText(position: Double) = "${formatTime(position)} / ${formatTime(state.seekDuration)}"

    private fun editDevice(dev: OperatorDevice) {
        val name = EditText(this).apply { hint = "Name"; setText(dev.name); setSingleLine() }
        val group = EditText(this).apply { hint = "Group"; setText(dev.group); setSingleLine() }
        val form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(8), dp(20), 0)
            addView(name, matchWrap())
            addView(group, matchWrap())
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle(dev.label)
            .setView(form)
            .setPositiveButton("Save") { _, _ -> vm.rename(dev.id, name.text.toString().trim(), group.text.toString().trim()) }
            .setNegativeButton("Cancel", null)
        if (!dev.online) dialog.setNeutralButton("Forget") { _, _ -> vm.forget(dev.id) }
        dialog.show()
    }

    // ---------------------------------------------------------------- adapters

    private abstract inner class RowAdapter<T> : BaseAdapter() {
        protected var items: List<T> = emptyList()
        protected var cur: OperatorUiState = OperatorUiState()
        fun item(position: Int): T? = items.getOrNull(position)
        override fun getCount() = items.size
        override fun getItem(position: Int): Any? = items.getOrNull(position)
        override fun getItemId(position: Int) = position.toLong()

        fun submit(s: OperatorUiState) {
            cur = s
            items = itemsOf(s)
            notifyDataSetChanged()
        }

        protected abstract fun itemsOf(s: OperatorUiState): List<T>
        protected abstract fun bind(item: T, title: TextView, detail: TextView, row: View)

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val row = (convertView as? LinearLayout) ?: LinearLayout(this@DashboardActivity).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(10), dp(8), dp(10), dp(8))
                addView(label("", 15f, bold = true), matchWrap())
                addView(label("", 12f), matchWrap())
            }
            val item = items[position]
            row.setBackgroundColor(Color.TRANSPARENT)
            bind(item, row.getChildAt(0) as TextView, row.getChildAt(1) as TextView, row)
            return row
        }
    }

    private inner class DevicesAdapter : RowAdapter<OperatorDevice>() {
        override fun itemsOf(s: OperatorUiState) = (s.snapshot?.devices ?: emptyList())
            .sortedWith(compareBy<OperatorDevice>({ !it.online }, { it.label.lowercase(Locale.ROOT) }))

        override fun bind(item: OperatorDevice, title: TextView, detail: TextView, row: View) {
            if (item.id in cur.selected) row.setBackgroundColor(SELECTED)
            title.text = (if (item.online) "● " else "○ ") + item.label + if (item.group.isNotEmpty()) "  [${item.group}]" else ""
            val st = item.status
            val parts = ArrayList<String>()
            parts.add(item.stateText)
            val video = st.video
            if (video != null) parts.add(video)
            val pos = st.position
            val dur = st.duration
            if (item.online && pos != null && dur != null && dur > 0) parts.add("${formatTime(pos)} / ${formatTime(dur)}")
            val drift = st.driftMs
            if (drift != null && st.state == "playing") parts.add("drift %+.0f ms".format(drift))
            val battery = st.battery
            if (battery != null) parts.add("battery ${Math.round(battery * 100)}%")
            val receiving = st.downloadFraction
            if (receiving != null) parts.add("receiving ${st.downloadName} ${Math.round(receiving * 100)}%")
            val error = st.error
            if (error != null) parts.add(error)
            detail.text = parts.joinToString("  ·  ")
        }
    }

    private inner class LibraryAdapter : RowAdapter<LibraryVideo>() {
        override fun itemsOf(s: OperatorUiState) = s.snapshot?.library ?: emptyList()

        override fun bind(item: LibraryVideo, title: TextView, detail: TextView, row: View) {
            if (item.name == cur.effectiveVideo) row.setBackgroundColor(SELECTED)
            title.text = item.displayName
            val snap = cur.snapshot
            val total = snap?.devices?.size ?: 0
            val have = snap?.haveCount(item) ?: 0
            detail.text = "${formatTime(item.duration)}  ·  ${formatBytes(item.size)}  ·  on $have of $total headsets"
        }
    }

    private inner class EventsAdapter : RowAdapter<OperatorEvent>() {
        private val clock = SimpleDateFormat("HH:mm:ss", Locale.US)

        override fun itemsOf(s: OperatorUiState) = (s.snapshot?.events ?: emptyList()).asReversed()

        override fun bind(item: OperatorEvent, title: TextView, detail: TextView, row: View) {
            val color = when (item.level) {
                "error" -> Color.rgb(176, 0, 32)
                "warn" -> Color.rgb(160, 90, 0)
                else -> Color.DKGRAY
            }
            title.setTextColor(color)
            title.text = item.message
            val who = item.device?.let { id -> cur.snapshot?.device(id)?.label ?: id }
            detail.text = clock.format(Date((item.time * 1000).toLong())) + if (who != null) "  ·  $who" else ""
        }
    }
}
