## Canlı Ölçümler

Page\: `Data -> Live Measurements`

Canlı Ölçümler sayfası, bir kullanıcının Mycodo'ya giriş yaptıktan sonra gördüğü ilk sayfadır. Giriş ve Fonksiyon kontrolörlerinden alınan mevcut ölçümleri görüntüler. Canlı` sayfasında hiçbir şey görüntülenmiyorsa, bir Giriş veya Fonksiyon kontrolörünün hem doğru yapılandırıldığından hem de etkinleştirildiğinden emin olun. Veriler ölçüm veritabanından otomatik olarak sayfada güncellenecektir.

## Senkronize olmayan grafikler

Sayfa\: `Veri -> Senkronize olmayan grafikler`

Senkron Grafik olarak görüntülemek için çok veri ve işlemci yoğun olabilecek nispeten uzun zaman dilimlerini (haftalar/aylar/yıllar) kapsayan veri kümelerini görüntülemek için yararlı olan bir grafik veri ekranı. Bir zaman dilimi seçtiğinizde, eğer varsa, o zaman aralığındaki veriler yüklenecektir. İlk görünüm seçilen veri setinin tamamına ait olacaktır. Her görünüm/yakınlaştırma için 700 veri noktası yüklenecektir. Seçilen zaman aralığı için 700'den fazla veri noktası kaydedilmişse, 700 nokta o zaman aralığındaki noktaların ortalamasından oluşturulacaktır. Bu, büyük bir veri setinde gezinmek için çok daha az verinin kullanılmasını sağlar. Örneğin, 4 aylık verinin tamamı indirilirse 10 megabayt olabilir. Ancak, 4 aylık bir süreyi görüntülerken, bu 10 megabaytın her veri noktasını görmek mümkün değildir ve noktaların toplanması kaçınılmazdır. Eşzamansız veri yüklemesi ile yalnızca gördüğünüz kadarını indirirsiniz. Böylece, her grafik yüklemesinde 10 megabayt indirmek yerine, yeni bir yakınlaştırma seviyesi seçilene kadar yalnızca ~50kb indirilir ve bu sırada yalnızca ~50kb daha indirilir.

!!! note
    Grafikler ölçümler gerektirir, bu nedenle verileri görüntülemek için en az bir Giriş/Çıkış/Fonksiyon/vb. eklenmesi ve etkinleştirilmesi gerekir.

## Gösterge Tablosu

Sayfa\: `Veri -> Gösterge Tablosu`

Gösterge paneli, mevcut çok sayıda gösterge paneli widget'ı sayesinde hem verileri görüntülemek hem de sistemi manipüle etmek için kullanılabilir. Birden fazla gösterge tablosu oluşturulabilir ve düzenlemenin değiştirilmesini önlemek için kilitlenebilir.

## Widget'lar

Pencere öğeleri, Gösterge Tablosunda veri görüntüleme (grafikler, göstergeler, gösterge saatleri, vb.) veya sistemle etkileşim (çıkışları manipüle etme, PWM görev döngüsünü değiştirme, bir veritabanını sorgulama veya değiştirme, vb.) Pencere öğeleri sürüklenip bırakılarak kolayca yeniden düzenlenebilir ve yeniden boyutlandırılabilir. Desteklenen Pencere Araçlarının tam listesi için [Desteklenen Pencere Araçları](Supported-Widgets.md) bölümüne bakın.

### Özel Widget'lar

Mycodo'da, kullanıcı tarafından oluşturulan Widget'ların Mycodo sisteminde kullanılmasına olanak tanıyan bir Özel Widget içe aktarma sistemi vardır. Özel Widget'lar `[Dişli Simgesi] -> Yapılandır -> Özel Widget'lar` sayfasından yüklenebilir. İçe aktarıldıktan sonra, `Ayar -> Widget` sayfasından kullanılabilirler.

Çalışan bir modül geliştirirseniz, lütfen [yeni bir GitHub sorunu oluşturmayı] (https://github.com/kizniche/Mycodo/issues/new?assignees=&labels=&template=feature-request.md&title=New%20Module) veya çekme talebinde bulunmayı düşünün; yerleşik sete dahil edilebilir.

Doğru biçimlendirme örnekleri için [Mycodo/mycodo/widgets](https://github.com/kizniche/Mycodo/tree/master/mycodo/widgets/) dizininde bulunan yerleşik Widget modüllerinden herhangi birini açın. Ayrıca [Mycodo/mycodo/widgets/examples](https://github.com/kizniche/Mycodo/tree/master/mycodo/widgets/examples) dizininde örnek Özel Widget'ler de bulunmaktadır.

Özel bir widget modülü oluşturmak genellikle Javascript'in özel olarak yerleştirilmesini ve yürütülmesini gerektirir. Bunu ele almak için her modülde çeşitli değişkenler oluşturuldu ve birden fazla widget görüntülenirken oluşturulacak gösterge tablosu sayfasının aşağıdaki kısa yapısını takip etti.

```angular2html
<html>
<head>
  <title>Title</title>
  <script>
    {{ widget_1_dashboard_head }}
    {{ widget_2_dashboard_head }}
  </script>
</head>
<body>

<div id="widget_1">
  <div id="widget_1_titlebar">{{ widget_dashboard_title_bar }}</div>
  {{ widget_1_dashboard_body }}
  <script>
    $(document).ready(function() {
      {{ widget_1_dashboard_js_ready_end }}
    });
  </script>
</div>

<div id="widget_2">
  <div id="widget_2_titlebar">{{ widget_dashboard_title_bar }}</div>
  {{ widget_2_dashboard_body }}
  <script>
    $(document).ready(function() {
      {{ widget_2_dashboard_js_ready_end }}
    });
  </script>
</div>

<script>
  {{ widget_1_dashboard_js }}
  {{ widget_2_dashboard_js }}

  $(document).ready(function() {
    {{ widget_1_dashboard_js_ready }}
    {{ widget_2_dashboard_js_ready }}
  });
</script>

</body>
</html>
```
