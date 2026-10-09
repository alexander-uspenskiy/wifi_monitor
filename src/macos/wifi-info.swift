import CoreWLAN
// Default:  prints "rssi noise channel band width txrate_mbps phymode" (or "NA" if not associated)
// "scan":   prints one JSON line {"own":[ch,band,width]|null,"nets":[[ch,band,rssi],...]}
func bandName(_ b: CWChannelBand) -> String { switch b { case .band2GHz: return "2.4"; case .band5GHz: return "5"; case .band6GHz: return "6"; default: return "?" } }
func widthMHz(_ w: CWChannelWidth) -> String { switch w { case .width20MHz: return "20"; case .width40MHz: return "40"; case .width80MHz: return "80"; case .width160MHz: return "160"; default: return "?" } }

guard let i = CWWiFiClient.shared().interface(), i.powerOn() else { print("NA"); exit(0) }

if CommandLine.arguments.contains("scan") {
  var own = "null"
  if let ch = i.wlanChannel(), i.rssiValue() != 0 { own = "[\(ch.channelNumber),\"\(bandName(ch.channelBand))\",\"\(widthMHz(ch.channelWidth))\"]" }
  guard let nets = try? i.scanForNetworks(withSSID: nil) else { print("{\"own\":\(own),\"nets\":[]}"); exit(0) }
  let items = nets.compactMap { n -> String? in
    guard let ch = n.wlanChannel else { return nil }
    return "[\(ch.channelNumber),\"\(bandName(ch.channelBand))\",\(n.rssiValue)]"
  }
  print("{\"own\":\(own),\"nets\":[\(items.joined(separator: ","))]}")
  exit(0)
}

guard let ch = i.wlanChannel(), i.rssiValue() != 0 else { print("NA"); exit(0) }
print("\(i.rssiValue()) \(i.noiseMeasurement()) \(ch.channelNumber) \(bandName(ch.channelBand)) \(widthMHz(ch.channelWidth)) \(Int(i.transmitRate())) \(i.activePHYMode().rawValue)")
