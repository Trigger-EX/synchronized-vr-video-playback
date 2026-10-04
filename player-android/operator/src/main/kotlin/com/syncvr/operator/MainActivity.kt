package com.syncvr.operator

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.TextView
import com.syncvr.player.core.net.JavaUdpReceiverFactory
import com.syncvr.player.core.operator.ApiException
import com.syncvr.player.core.operator.FoundServer
import com.syncvr.player.core.operator.OperatorApi
import com.syncvr.player.core.operator.ServerEndpoint
import com.syncvr.player.core.operator.ServerFinder
import java.util.concurrent.Executors

/** Connect screen: servers found by their UDP beacon, or an address typed in by hand. */
class MainActivity : Activity() {
    private val ui = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private val servers = ArrayList<FoundServer>()
    private lateinit var adapter: ArrayAdapter<String>
    private lateinit var address: EditText
    private lateinit var password: EditText
    private lateinit var status: TextView
    private lateinit var connectButton: Button
    private lateinit var scanButton: Button
    @Volatile private var scanning = false
    @Volatile private var destroyed = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "SyncVR Operator"
        val prefs = getSharedPreferences("operator", Context.MODE_PRIVATE)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(16), dp(16), dp(16))
        }
        root.addView(label("Servers on this network", 18f, bold = true), matchWrap())

        status = label("", 13f)
        root.addView(status, matchWrap())

        adapter = ArrayAdapter(this, android.R.layout.simple_list_item_1, ArrayList<String>())
        val list = ListView(this).apply {
            this.adapter = this@MainActivity.adapter
            setOnItemClickListener { _, _, position, _ ->
                servers.getOrNull(position)?.let { address.setText("${it.host}:${it.httpPort}") }
            }
        }
        root.addView(list, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))

        scanButton = button("Search again") { scan() }
        root.addView(scanButton, matchWrap())

        root.addView(label("Or enter the address", 14f, bold = true), matchWrap())
        address = EditText(this).apply {
            hint = "192.168.1.10 or 192.168.1.10:8080"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine()
            setText(prefs.getString("address", ""))
        }
        root.addView(address, matchWrap())
        password = EditText(this).apply {
            hint = "Password (only if the server has one)"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setSingleLine()
            setText(prefs.getString("password", ""))
        }
        root.addView(password, matchWrap())
        connectButton = button("Connect") { connect() }
        root.addView(connectButton, matchWrap())

        setContentView(root)
        scan()
    }

    override fun onDestroy() {
        destroyed = true
        worker.shutdownNow()
        super.onDestroy()
    }

    private fun scan() {
        if (scanning) return
        scanning = true
        servers.clear()
        adapter.clear()
        scanButton.isEnabled = false
        status.text = "Searching..."
        worker.execute {
            var error: String? = null
            try {
                ServerFinder(JavaUdpReceiverFactory, guard = MulticastLockGuard(this)).scan(
                    windowMs = 5000,
                    keepGoing = { !destroyed },
                    onFound = { found -> ui.post { if (!destroyed) addServer(found) } },
                )
            } catch (e: Exception) {
                error = e.message ?: e.javaClass.simpleName
            }
            scanning = false
            ui.post {
                if (destroyed) return@post
                scanButton.isEnabled = true
                status.text = when {
                    error != null -> "Search failed: $error"
                    servers.isEmpty() -> "No server found. Check that this device is on the same Wi-Fi, or enter the address."
                    else -> "Tap a server to use it."
                }
            }
        }
    }

    private fun addServer(found: FoundServer) {
        servers.add(found)
        adapter.add(found.label)
        if (address.text.isNullOrBlank()) address.setText("${found.host}:${found.httpPort}")
    }

    private fun connect() {
        val endpoint = ServerEndpoint.parse(address.text.toString(), password.text.toString())
        if (endpoint == null) {
            address.error = "Enter an address like 192.168.1.10"
            return
        }
        connectButton.isEnabled = false
        status.text = "Connecting to ${endpoint.label}..."
        worker.execute {
            val failure: ApiException? = try {
                OperatorApi(endpoint).state()
                null
            } catch (e: ApiException) {
                e
            } catch (e: Exception) {
                ApiException(0, e.message ?: "cannot reach ${endpoint.label}")
            }
            ui.post {
                if (destroyed) return@post
                connectButton.isEnabled = true
                when {
                    failure == null -> open(endpoint)
                    failure.unauthorized -> {
                        status.text = "This server needs a password."
                        password.error = "Wrong or missing password"
                        password.requestFocus()
                    }
                    else -> status.text = "Cannot connect: ${failure.message}"
                }
            }
        }
    }

    private fun open(endpoint: ServerEndpoint) {
        getSharedPreferences("operator", Context.MODE_PRIVATE).edit()
            .putString("address", "${endpoint.host}:${endpoint.httpPort}")
            .putString("password", endpoint.password)
            .apply()
        startActivity(
            Intent(this, DashboardActivity::class.java)
                .putExtra(DashboardActivity.EXTRA_HOST, endpoint.host)
                .putExtra(DashboardActivity.EXTRA_PORT, endpoint.httpPort)
                .putExtra(DashboardActivity.EXTRA_PASSWORD, endpoint.password),
        )
    }
}
