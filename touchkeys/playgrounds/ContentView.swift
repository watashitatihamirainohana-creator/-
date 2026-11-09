// touchkeys — Swift Playgrounds 版 (iPad をPC音ゲーの鍵盤コントローラーにする)
//
// 使い方: Swift Playgrounds で「App」を新規作成し、ContentView.swift の中身を
// このファイルの内容で丸ごと置き換えて実行する。PC 側は touchkeys.pyw を起動しておく。

import SwiftUI
import UIKit
import Network

// MARK: - PC との通信 (素の TCP、1 行 1 メッセージ)

final class PCLink: ObservableObject, @unchecked Sendable {
    @Published var connected = false
    @Published var status = "未接続"
    @Published var keys: [String] = []
    var slide = false
    var retrigger = true

    private var conn: NWConnection?
    private var buffer = Data()
    private var address = ""
    private var wanted = false
    private var generation = 0

    /// address は "192.168.3.8" または "192.168.3.8:8766"
    func start(address: String) {
        self.address = address.trimmingCharacters(in: .whitespacesAndNewlines)
        wanted = true
        open()
    }

    func stop() {
        wanted = false
        generation += 1
        conn?.cancel()
        conn = nil
        connected = false
        keys = []
        status = "未接続"
    }

    func send(_ line: String) {
        guard connected, let c = conn, let data = (line + "\n").data(using: .utf8) else { return }
        c.send(content: data, completion: .contentProcessed { _ in })
    }

    private func open() {
        generation += 1
        let gen = generation
        conn?.cancel()
        conn = nil
        buffer.removeAll()

        var host = address
        var portNumber: UInt16 = 8766
        let parts = address.split(separator: ":")
        if parts.count == 2, let p = UInt16(parts[1]) {
            host = String(parts[0])
            portNumber = p
        }
        guard !host.isEmpty, let port = NWEndpoint.Port(rawValue: portNumber) else {
            status = "アドレスを入力してください"
            return
        }

        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true          // 小さいパケットをためずにすぐ送る
        let params = NWParameters(tls: nil, tcp: tcp)
        let c = NWConnection(host: NWEndpoint.Host(host), port: port, using: params)
        conn = c
        status = "接続中… \(host):\(portNumber)"

        c.stateUpdateHandler = { [weak self] state in
            guard let self = self, gen == self.generation else { return }
            switch state {
            case .ready:
                self.status = "接続しました。設定を受信中…"
                self.receive(c, gen)
            case .failed(let error):
                self.lost("接続できません: \(error.localizedDescription)")
            case .waiting(let error):
                self.lost("PC が見つかりません: \(error.localizedDescription)")
            default:
                break
            }
        }
        c.start(queue: .main)
    }

    private func receive(_ c: NWConnection, _ gen: Int) {
        c.receive(minimumIncompleteLength: 1, maximumLength: 4096) { [weak self] data, _, isComplete, error in
            guard let self = self, gen == self.generation else { return }
            if let data = data, !data.isEmpty {
                self.buffer.append(data)
                self.parse()
            }
            if isComplete || error != nil {
                self.lost("PC との接続が切れました")
                return
            }
            self.receive(c, gen)
        }
    }

    /// サーバーから届くのは設定の JSON 1 行だけ
    private func parse() {
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = Data(buffer[buffer.startIndex..<nl])
            buffer = Data(buffer[buffer.index(after: nl)...])
            guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let k = obj["keys"] as? [String] else { continue }
            slide = (obj["slide"] as? Bool) ?? false
            retrigger = (obj["retrigger"] as? Bool) ?? true
            keys = k
            connected = true
            status = "接続中"
        }
    }

    private func lost(_ message: String) {
        generation += 1
        let gen = generation
        conn?.cancel()
        conn = nil
        connected = false
        keys = []
        status = message + "(再接続中…)"
        guard wanted else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self = self, gen == self.generation, self.wanted else { return }
            self.open()
        }
    }
}

// MARK: - タッチ面 (UIKit でタッチを直接受ける)

final class PadView: UIView {
    var link: PCLink?

    private var keys: [String] = []
    private var laneViews: [UIView] = []
    private var nameLabels: [UILabel] = []
    private var countLabels: [UILabel] = []
    private var active: [UITouch: Int] = [:]   // 触れている指 → レーン
    private var counts: [Int] = []             // レーンごとの触れている指の数
    private var seen: [Int] = []               // レーンごとの叩いた回数 (確認用)
    private var out: [String] = []

    private let offColors = [UIColor(white: 0.07, alpha: 1), UIColor(white: 0.10, alpha: 1)]
    private let onColor = UIColor(red: 0.18, green: 0.49, blue: 0.96, alpha: 1)

    override init(frame: CGRect) {
        super.init(frame: frame)
        isMultipleTouchEnabled = true
        isExclusiveTouch = false
        backgroundColor = .black
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) は使いません")
    }

    func configure(_ newKeys: [String]) {
        if newKeys == keys { return }
        keys = newKeys
        active.removeAll()
        out.removeAll()
        counts = Array(repeating: 0, count: keys.count)
        seen = Array(repeating: 0, count: keys.count)
        laneViews.forEach { $0.removeFromSuperview() }
        laneViews = []
        nameLabels = []
        countLabels = []
        for (i, key) in keys.enumerated() {
            let lane = UIView()
            lane.isUserInteractionEnabled = false
            lane.backgroundColor = offColors[i % 2]
            let name = UILabel()
            name.text = key.uppercased()
            name.textAlignment = .center
            name.textColor = UIColor(white: 0.55, alpha: 1)
            name.font = .systemFont(ofSize: 44, weight: .bold)
            name.adjustsFontSizeToFitWidth = true
            let count = UILabel()
            count.text = "0"
            count.textAlignment = .center
            count.textColor = UIColor(white: 0.45, alpha: 1)
            count.font = .monospacedDigitSystemFont(ofSize: 20, weight: .regular)
            lane.addSubview(name)
            lane.addSubview(count)
            addSubview(lane)
            laneViews.append(lane)
            nameLabels.append(name)
            countLabels.append(count)
        }
        setNeedsLayout()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        guard !laneViews.isEmpty else { return }
        let w = bounds.width / CGFloat(laneViews.count)
        let h = bounds.height
        for (i, lane) in laneViews.enumerated() {
            lane.frame = CGRect(x: CGFloat(i) * w + 0.5, y: 0, width: w - 1, height: h)
            nameLabels[i].frame = CGRect(x: 4, y: h - 150, width: w - 9, height: 56)
            countLabels[i].frame = CGRect(x: 4, y: h - 92, width: w - 9, height: 28)
        }
    }

    private func lane(of touch: UITouch) -> Int {
        let n = keys.count
        let i = Int(touch.location(in: self).x / max(bounds.width, 1) * CGFloat(n))
        return min(max(i, 0), n - 1)
    }

    private func press(_ i: Int) {
        seen[i] += 1
        countLabels[i].text = String(seen[i])
        counts[i] += 1
        if counts[i] == 1 {
            out.append("d\(i)")
            laneViews[i].backgroundColor = onColor
        } else if link?.retrigger ?? true {
            out.append("r\(i)")      // 押さえ中のレーンを別の指で叩いた → 打ち直す
        }
    }

    private func release(_ i: Int) {
        guard counts[i] > 0 else { return }
        counts[i] -= 1
        if counts[i] == 0 {
            out.append("u\(i)")
            laneViews[i].backgroundColor = offColors[i % 2]
        }
    }

    private func flush() {
        if !out.isEmpty {
            link?.send(out.joined(separator: " "))
            out.removeAll()
        }
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard !keys.isEmpty else { return }
        for t in touches {
            // 同じ場所を連打すると iOS は同じ UITouch を使い回すので、残っていたら先に離す
            if let old = active[t] { release(old) }
            let i = lane(of: t)
            active[t] = i
            press(i)
        }
        flush()
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard !keys.isEmpty, link?.slide == true else { return }
        for t in touches {
            guard let old = active[t] else { continue }
            let i = lane(of: t)
            if i != old {
                active[t] = i
                release(old)
                press(i)
            }
        }
        flush()
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        end(touches)
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        end(touches)
    }

    private func end(_ touches: Set<UITouch>) {
        guard !keys.isEmpty else { return }
        for t in touches {
            if let i = active.removeValue(forKey: t) { release(i) }
        }
        flush()
    }
}

struct Pad: UIViewRepresentable {
    @ObservedObject var link: PCLink

    func makeUIView(context: Context) -> PadView {
        let view = PadView(frame: .zero)
        view.link = link
        return view
    }

    func updateUIView(_ view: PadView, context: Context) {
        view.configure(link.keys)
    }
}

// MARK: - 画面

/// 画面端のシステムジェスチャー (ホームへ戻る・通知センター) を 1 回目のスワイプでは発動させない
struct GameScreen: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 16.0, *) {
            content
                .defersSystemGestures(on: .all)
                .persistentSystemOverlays(.hidden)
        } else {
            content
        }
    }
}

struct ContentView: View {
    @StateObject private var link = PCLink()
    @AppStorage("touchkeys.address") private var address = ""

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            Pad(link: link).ignoresSafeArea()
            if !link.connected {
                panel
            }
        }
        .modifier(GameScreen())
        .statusBar(hidden: true)
        .onAppear {
            if !address.isEmpty { link.start(address: address) }
        }
    }

    private var panel: some View {
        VStack(spacing: 16) {
            Text("touchkeys")
                .font(.largeTitle.bold())
            Text("PC で touchkeys を起動し、ウィンドウの「アプリ版」に出ているアドレスを入力してください。")
                .multilineTextAlignment(.center)
                .foregroundColor(.secondary)
            TextField("例: 192.168.3.8", text: $address)
                .textFieldStyle(.roundedBorder)
                .keyboardType(.numbersAndPunctuation)
                .disableAutocorrection(true)
                .frame(maxWidth: 320)
            Button("接続") {
                link.start(address: address)
            }
            .buttonStyle(.borderedProminent)
            Text(link.status)
                .font(.footnote)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(28)
        .frame(maxWidth: 460)
        .background(Color(UIColor.secondarySystemBackground))
        .cornerRadius(20)
        .padding()
    }
}
