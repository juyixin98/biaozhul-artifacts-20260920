package registry

import (
	"strings"
	"testing"

	"depsolver/internal/model"
	"depsolver/internal/semver"
)

func pkg(name string, versions ...model.PackageVersion) model.Package {
	return model.Package{Name: name, Versions: versions}
}

func ver(v string, deps ...model.Dependency) model.PackageVersion {
	return model.PackageVersion{Version: v, Deps: deps}
}

func TestBuildSortsVersionsDescending(t *testing.T) {
	reg, err := Build(model.RegistryInput{Packages: []model.Package{
		pkg("lib", ver("1.0.0"), ver("1.10.0"), ver("1.2.0"), ver("2.0.0-alpha.1"), ver("2.0.0")),
	}})
	if err != nil {
		t.Fatal(err)
	}
	got := reg.Package("lib").Versions
	want := []string{"2.0.0", "2.0.0-alpha.1", "1.10.0", "1.2.0", "1.0.0"}
	if len(got) != len(want) {
		t.Fatalf("got %d versions", len(got))
	}
	for i := range want {
		if got[i].Raw != want[i] {
			t.Errorf("position %d: got %s want %s (order=%v)", i, got[i].Raw, want[i],
				[]string{got[0].Raw, got[1].Raw, got[2].Raw, got[3].Raw, got[4].Raw})
		}
	}
}

func TestBuildValidationErrors(t *testing.T) {
	cases := []struct {
		name string
		in   model.RegistryInput
		want string
	}{
		{
			"bad package name",
			model.RegistryInput{Packages: []model.Package{pkg("UPPER", ver("1.0.0"))}},
			"invalid package name",
		},
		{
			"duplicate package",
			model.RegistryInput{Packages: []model.Package{
				pkg("a", ver("1.0.0")), pkg("a", ver("2.0.0")),
			}},
			"duplicate package",
		},
		{
			"bad version",
			model.RegistryInput{Packages: []model.Package{pkg("a", ver("nope"))}},
			"invalid version",
		},
		{
			"duplicate version",
			model.RegistryInput{Packages: []model.Package{pkg("a", ver("1.0.0"), ver("1.0.0"))}},
			"duplicate version",
		},
		{
			"bad dependency name",
			model.RegistryInput{Packages: []model.Package{
				pkg("a", ver("1.0.0", model.Dependency{Name: "BAD", Constraint: "^1"})),
			}},
			"invalid dependency name",
		},
		{
			"bad dependency constraint",
			model.RegistryInput{Packages: []model.Package{
				pkg("a", ver("1.0.0", model.Dependency{Name: "b", Constraint: ">>1"})),
			}},
			"invalid version constraint",
		},
		{
			"duplicate dependency",
			model.RegistryInput{Packages: []model.Package{
				pkg("a", ver("1.0.0",
					model.Dependency{Name: "b", Constraint: "^1"},
					model.Dependency{Name: "b", Constraint: "^2"})),
			}},
			"duplicate dependency",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Build(tc.in)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error=%v want contains %q", err, tc.want)
			}
		})
	}
}

func TestMissingDependencyPackageIsAllowedAtBuild(t *testing.T) {
	// 依赖的包是否存在是求解期的问题；注册表构建不应因此失败。
	reg, err := Build(model.RegistryInput{Packages: []model.Package{
		pkg("a", ver("1.0.0", model.Dependency{Name: "ghost", Constraint: "^1.0.0"})),
	}})
	if err != nil {
		t.Fatal(err)
	}
	if reg.Package("ghost") != nil {
		t.Fatal("ghost should be absent from registry")
	}
	if names := reg.Names(); len(names) != 1 || names[0] != "a" {
		t.Fatalf("names=%v", names)
	}
}

func TestDependenciesParsed(t *testing.T) {
	reg, err := Build(model.RegistryInput{Packages: []model.Package{
		pkg("a", ver("1.0.0", model.Dependency{Name: "b", Constraint: ">=1.2.0 <2.0.0"})),
	}})
	if err != nil {
		t.Fatal(err)
	}
	deps := reg.Package("a").Versions[0].Deps
	if len(deps) != 1 {
		t.Fatalf("got %d deps", len(deps))
	}
	v, _ := semver.Parse("1.5.0")
	if !deps[0].Constraint.Satisfy(v, false) {
		t.Fatal("parsed constraint should accept 1.5.0")
	}
}

func TestValidName(t *testing.T) {
	good := []string{"a", "foo.bar", "foo_bar", "foo-bar", "@scope/pkg", "a1", "@s/p.x"}
	bad := []string{"", "A", "-bad", "@scope", "@/pkg", "a/b/c", "pkg/sub", "a b"}
	for _, n := range good {
		if !ValidName(n) {
			t.Errorf("expected valid: %q", n)
		}
	}
	for _, n := range bad {
		if ValidName(n) {
			t.Errorf("expected invalid: %q", n)
		}
	}
}
