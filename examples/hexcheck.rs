// 临时核对：打印样例报文的实际编码
fn main() {
    use mqtt_subset::codec::encode;
    use mqtt_subset::packet::*;
    let hex = |p: &Packet| encode(p).iter().map(|b| format!("{b:02X}")).collect::<Vec<_>>().join(" ");
    let c = Packet::Connect(Connect{client_id:"sub1".into(),clean_session:true,keep_alive:60,will:None,username:None,password:None});
    println!("CONNECT    {}", hex(&c));
    let s = Packet::Subscribe(Subscribe{packet_id:1,topics:vec![("sensors/+".into(),1)]});
    println!("SUBSCRIBE  {}", hex(&s));
    let mut pub1 = Publish::new("sensors/temp",1,b"21.5".to_vec()); pub1.retain=true; pub1.packet_id=Some(1);
    println!("PUBLISH    {}", hex(&Packet::Publish(pub1)));
    let mut d = Publish::new("sensors/temp",1,b"21.5".to_vec()); d.packet_id=Some(1);
    println!("DELIVERY   {}", hex(&Packet::Publish(d)));
    println!("CONNACK    {}", hex(&Packet::ConnAck(ConnAck{session_present:false,return_code:0})));
    println!("SUBACK     {}", hex(&Packet::SubAck(SubAck{packet_id:1,granted:vec![1]})));
    println!("PUBACK     {}", hex(&Packet::PubAck(1)));
    println!("PINGREQ    {}", hex(&Packet::PingReq));
    println!("PINGRESP   {}", hex(&Packet::PingResp));
    println!("DISCONNECT {}", hex(&Packet::Disconnect));
}
